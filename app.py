import os
import uuid
import json
import base64
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
UPLOAD_DIR = Path("/tmp/videobot_jobs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

VISION_PROMPT = """These images are from the same TikTok/Reel video.

Detect EVERY text element added as an overlay (captions, titles, stickers) — NOT text on clothing, posters, or objects in the scene.

Return a JSON array. Each element = one independent text object.

For EACH object return:
- "text": exact content with ALL emojis. Use \\n for line breaks WITHIN the same visual block.
- "cx_pct": CENTER x as fraction of frame width  (0.0=left edge, 1.0=right edge, 0.5=center)
- "cy_pct": CENTER y as fraction of frame height (0.0=top, 1.0=bottom)
- "w_pct":  estimated width of the text block as fraction of frame width
- "fontsize_pct": font height as fraction of frame height (e.g. 0.04 = 4% of height)
- "align": "left" | "center" | "right"
- "bold": true | false
- "color": "white" | "black" | other if clearly different

KEY RULES:
1. Each independently-positioned element = separate JSON object with its own coordinates.
2. A grid of numbers (e.g. keypad 1-9) = 9 separate objects, each at their own cx_pct/cy_pct.
3. Multi-line blocks that belong together visually = ONE object with \\n between lines.
4. Preserve ALL emojis exactly.
5. Return ONLY valid JSON array, no markdown, no explanation.

Example for a video with centered text + a keypad:
[
  {"text": "Type this fast", "cx_pct": 0.5, "cy_pct": 0.08, "w_pct": 0.7, "fontsize_pct": 0.04, "align": "center", "bold": false, "color": "white"},
  {"text": "1", "cx_pct": 0.18, "cy_pct": 0.55, "w_pct": 0.08, "fontsize_pct": 0.08, "align": "center", "bold": false, "color": "white"},
  {"text": "2", "cx_pct": 0.50, "cy_pct": 0.55, "w_pct": 0.08, "fontsize_pct": 0.08, "align": "center", "bold": false, "color": "white"}
]"""


# ── Global JSON error handler ─────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Fichier trop grand (max 200 MB)"}), 413


# ── Helpers ───────────────────────────────────────────────────────

def get_video_dims(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=30
        )
        for s in json.loads(r.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return int(s["width"]), int(s["height"])
    except Exception:
        pass
    return 576, 1024


def extract_frames(video_path: str, count: int = 4) -> list:
    """Extract evenly-spaced frames from video."""
    # Get duration
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-print_format", "json", video_path],
        capture_output=True, text=True, timeout=15
    )
    try:
        duration = float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        duration = 3.0

    frames = []
    step = max(0.5, duration / (count + 1))
    for i in range(1, count + 1):
        t = min(step * i, duration - 0.1)
        out = f"/tmp/frame_{uuid.uuid4().hex}.png"
        subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", video_path,
             "-vframes", "1", "-vf", "scale=720:-1", "-y", out],
            capture_output=True, timeout=15
        )
        if Path(out).exists():
            frames.append(out)
    return frames


def analyze_with_claude_vision(frame_paths: list) -> list:
    """Use Claude Vision to detect text blocks with emojis and positions."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []

    import anthropic

    content = []
    for path in frame_paths[:4]:
        try:
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64}
            })
        except Exception:
            pass

    if not content:
        return []

    content.append({"type": "text", "text": VISION_PROMPT})

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}]
    )

    raw = response.content[0].text.strip()
    # Remove markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    blocks = json.loads(raw)

    # Normalize: handle both old y_pct/x_pct and new cx_pct/cy_pct schemas
    normalized = []
    for block in blocks:
        text = block.get("text", "").strip()
        if not text:
            continue
        b = dict(block)
        # Support both naming conventions
        if "cx_pct" not in b:
            b["cx_pct"] = b.get("x_pct", 0.5)
        if "cy_pct" not in b:
            b["cy_pct"] = b.get("y_pct", 0.5)
        if "fontsize_pct" not in b:
            # Convert fontsize_b (px in 1280 frame) to fraction
            b["fontsize_pct"] = b.get("fontsize_b", 36) / 1280
        normalized.append(b)

    normalized.sort(key=lambda l: l.get("cy_pct", 0))
    return normalized


def analyze_with_tesseract_fallback(frame_paths: list) -> list:
    """Fallback OCR using Tesseract when no API key."""
    import numpy as np
    import pytesseract
    from PIL import Image

    if not frame_paths:
        return []

    img  = Image.open(frame_paths[0]).convert("RGB")
    arr  = np.array(img)
    h, w = arr.shape[:2]

    white = (arr[:,:,0] > 175) & (arr[:,:,1] > 175) & (arr[:,:,2] > 175)
    inv   = np.full((h, w), 255, dtype=np.uint8)
    inv[white] = 0
    from PIL import Image as PIL
    pil_inv = PIL.fromarray(inv).resize((w*2, h*2), PIL.NEAREST)

    data = pytesseract.image_to_data(pil_inv, config="--psm 3 --oem 3",
                                      output_type=pytesseract.Output.DICT)
    raw_words = []
    for i in range(len(data['text'])):
        txt = data['text'][i].strip()
        if not txt or int(data['conf'][i]) < 20: continue
        wx, wy = data['left'][i]//2, data['top'][i]//2
        ww, wh = data['width'][i]//2, data['height'][i]//2
        if wh < 4 or ww < 2: continue
        raw_words.append({'text': txt, 'x': wx, 'y': wy, 'w': ww, 'h': wh})

    if not raw_words: return []
    import numpy as np2
    med_h = float(np2.median([wd['h'] for wd in raw_words]))
    words = [wd for wd in raw_words if med_h*0.40 <= wd['h'] <= med_h*2.5]
    if not words: return []
    med_h2 = float(np2.median([wd['h'] for wd in words]))

    words.sort(key=lambda wd: wd['y'])
    groups = [[words[0]]]
    for wd in words[1:]:
        if abs(wd['y'] - groups[-1][-1]['y']) < med_h2 * 0.7:
            groups[-1].append(wd)
        else:
            groups.append([wd])

    lines = []
    for grp in groups:
        grp.sort(key=lambda wd: wd['x'])
        text = " ".join(wd['text'] for wd in grp)
        alpha_r = sum(c.isalpha() for c in text) / max(1, len(text))
        if alpha_r < 0.25 and len(text) < 4: continue
        y_l = min(wd['y'] for wd in grp) / h
        x_l = min(wd['x'] for wd in grp) / w
        font_est = max(10, int(np2.median([wd['h'] for wd in grp]) * 0.90))
        lines.append({"text": text, "y_pct": round(y_l,4),
                      "x_pct": round(max(0,(x_l-5)/w),4),
                      "fontsize_b": font_est, "align": "left", "bold": False})
    lines.sort(key=lambda l: l["y_pct"])
    return lines


def render_text_overlay(blocks: list, wa: int, ha: int, wb: int, hb: int) -> str:
    """
    Render all text objects onto a transparent RGBA image.
    Uses cx_pct/cy_pct as CENTER coordinates, supports multi-line with \\n.
    Returns path to the PNG overlay file.
    """
    from PIL import Image, ImageDraw, ImageFont

    overlay = Image.new("RGBA", (wa, ha), (0, 0, 0, 0))

    use_pilmoji = True
    try:
        from pilmoji import Pilmoji
    except ImportError:
        use_pilmoji = False

    for block in blocks:
        text = block.get("text", "").strip()
        if not text:
            continue

        # Font size: fontsize_pct is fraction of frame height
        fontsize_pct = block.get("fontsize_pct", 0.035)
        fontsize     = max(10, int(ha * fontsize_pct))

        # Center coordinates in pixels
        cx = int(wa * block.get("cx_pct", 0.5))
        cy = int(ha * block.get("cy_pct", 0.5))

        bold      = block.get("bold", False)
        color_str = block.get("color", "white")
        color = (255, 255, 255, 255) if "white" in color_str.lower() else (0, 0, 0, 255)

        font_path = FONT_BOLD if bold else FONT_REG
        try:
            font = ImageFont.truetype(font_path, fontsize)
        except Exception:
            font = ImageFont.load_default()

        border = max(1, fontsize // 10)
        shadow = (0, 0, 0, 210)

        # Handle multi-line blocks
        lines  = text.split("\\n")
        line_h = int(fontsize * 1.25)
        total_h = len(lines) * line_h
        y_start = cy - total_h // 2

        def draw_line(canvas, x, y, line_text, fnt):
            # Draw border/shadow
            for dx in range(-border, border + 1):
                for dy in range(-border, border + 1):
                    if dx == 0 and dy == 0:
                        continue
                    if abs(dx) + abs(dy) <= border + 1:
                        canvas.text((x + dx, y + dy), line_text, font=fnt, fill=shadow)
            canvas.text((x, y), line_text, font=fnt, fill=color)

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            y = y_start + i * line_h

            # Compute x for this line (centered on cx)
            try:
                bbox = font.getbbox(line)
                tw   = bbox[2] - bbox[0]
            except Exception:
                tw = len(line) * fontsize // 2

            x = max(0, cx - tw // 2)
            x = min(x, wa - 1)  # don't go off-screen
            y = max(0, min(y, ha - fontsize - 1))

            if use_pilmoji:
                try:
                    with Pilmoji(overlay) as pm:
                        # shadow
                        for dx in range(-border, border + 1):
                            for dy in range(-border, border + 1):
                                if abs(dx) + abs(dy) <= border + 1 and (dx or dy):
                                    pm.text((x+dx, y+dy), line, font=font, fill=shadow)
                        pm.text((x, y), line, font=font, fill=color)
                except Exception:
                    use_pilmoji = False

            if not use_pilmoji:
                draw = ImageDraw.Draw(overlay)
                draw_line(draw, x, y, line, font)

    out_path = f"/tmp/overlay_{uuid.uuid4().hex}.png"
    overlay.save(out_path, "PNG")
    return out_path


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """Upload Video B → detect text blocks via Claude Vision (or Tesseract fallback)."""
    try:
        if "video_b" not in request.files:
            return jsonify({"error": "video_b manquant"}), 400

        vb     = request.files["video_b"]
        tmp_id = str(uuid.uuid4())
        tmp    = UPLOAD_DIR / tmp_id
        tmp.mkdir(parents=True, exist_ok=True)
        path_b = str(tmp / "b.mp4")
        vb.save(path_b)

        # Extract multiple frames
        frames = extract_frames(path_b, count=4)

        # Try Claude Vision first
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
        if has_key:
            lines = analyze_with_claude_vision(frames)
        else:
            lines = []

        # Fallback to Tesseract if Vision failed or no key
        if not lines:
            lines = analyze_with_tesseract_fallback(frames)

        # Cleanup temp frames
        for f in frames:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass

        _, hb = get_video_dims(path_b)
        return jsonify({
            "lines":          lines,
            "video_b_height": hb,
            "mode":           "vision" if has_key else "tesseract"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/process", methods=["POST"])
def process():
    try:
        if "video_a" not in request.files or "video_b" not in request.files:
            return jsonify({"error": "Les deux videos sont requises."}), 400

        va = request.files["video_a"]
        vb = request.files["video_b"]

        job_id  = str(uuid.uuid4())
        job_dir = UPLOAD_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        path_a   = str(job_dir / "a.mp4")
        path_b   = str(job_dir / "b.mp4")
        path_out = str(job_dir / "c.mp4")

        va.save(path_a)
        vb.save(path_b)

        wa, ha = get_video_dims(path_a)
        wb, hb = get_video_dims(path_b)

        # Get text lines from form
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines = json.loads(lines_json)
        except Exception:
            lines = []

        if not lines:
            return jsonify({"error": "Aucune ligne de texte fournie."}), 400

        # Render text overlay image (supports emojis via Pilmoji)
        overlay_path = render_text_overlay(lines, wa, ha, wb, hb)

        # Compose: video A + overlay image + audio from B
        cmd = [
            "ffmpeg", "-y",
            "-i", path_a,
            "-i", path_b,
            "-i", overlay_path,
            "-filter_complex",
            "[0:v][2:v]overlay=0:0[out]",
            "-map", "[out]",
            "-map", "1:a",
            "-shortest",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
            "-loglevel", "error",
            path_out
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Timeout (>3 min)"}), 500
        finally:
            try:
                Path(overlay_path).unlink(missing_ok=True)
            except Exception:
                pass

        if proc.returncode != 0 or not Path(path_out).exists():
            err = proc.stderr[-800:] if proc.stderr else "ffmpeg a echoue"
            return jsonify({"error": err}), 500

        return jsonify({"job_id": job_id})

    except Exception as e:
        return jsonify({"error": f"Erreur serveur: {str(e)}"}), 500


@app.route("/download/<job_id>")
def download(job_id):
    if ".." in job_id or "/" in job_id:
        return jsonify({"error": "Invalid"}), 400
    path = UPLOAD_DIR / job_id / "c.mp4"
    if path.exists():
        return send_file(str(path), as_attachment=True,
                         download_name="video_C.mp4", mimetype="video/mp4")
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
