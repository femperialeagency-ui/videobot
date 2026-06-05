import os
import uuid
import json
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
UPLOAD_DIR = Path("/tmp/videobot_jobs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

FONT      = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


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


def analyze_text_lines(video_path: str) -> list:
    """
    Detect text lines using full-frame OCR with word bounding boxes.
    Returns list of {text, y_pct, x_pct, fontsize_b} sorted top→bottom.
    """
    frame_path = f"/tmp/analyze_{uuid.uuid4().hex}.png"
    try:
        import numpy as np
        import pytesseract
        from PIL import Image

        subprocess.run(
            ["ffmpeg", "-ss", "1", "-i", video_path,
             "-vframes", "1", "-y", frame_path],
            capture_output=True, timeout=15
        )
        if not Path(frame_path).exists():
            return []

        img = Image.open(frame_path).convert("RGB")
        arr = np.array(img)
        h, w = arr.shape[:2]

        # Invert full frame: white text → black (Tesseract sweet spot)
        white_mask = (arr[:,:,0] > 175) & (arr[:,:,1] > 175) & (arr[:,:,2] > 175)
        inv = np.full((h, w), 255, dtype=np.uint8)
        inv[white_mask] = 0

        # Scale 2× for better OCR accuracy
        pil_inv = Image.fromarray(inv).resize((w * 2, h * 2), Image.NEAREST)

        # Get all words with bounding boxes
        data = pytesseract.image_to_data(
            pil_inv, config="--psm 3 --oem 3",
            output_type=pytesseract.Output.DICT
        )

        # Collect valid words (convert coords back to original frame scale)
        words = []
        for i in range(len(data['text'])):
            txt  = data['text'][i].strip()
            conf = int(data['conf'][i])
            if not txt or conf < 20:
                continue
            wx = data['left'][i]   // 2
            wy = data['top'][i]    // 2
            ww = data['width'][i]  // 2
            wh = data['height'][i] // 2
            # Skip tiny or abnormally tall/thin bboxes (OCR artifacts)
            if wh < 5 or ww < 3:
                continue
            if wh > h * 0.12:          # taller than 12% of frame → artifact
                continue
            words.append({'text': txt, 'x': wx, 'y': wy, 'w': ww, 'h': wh})

        if not words:
            return []

        # Compute median word height (for proximity threshold)
        med_h = float(np.median([wd['h'] for wd in words]))

        # Group words into lines by y-proximity
        words.sort(key=lambda wd: wd['y'])
        groups = [[words[0]]]
        for wd in words[1:]:
            if abs(wd['y'] - groups[-1][-1]['y']) < med_h * 0.7:
                groups[-1].append(wd)
            else:
                groups.append([wd])

        # Build line objects
        lines = []
        for grp in groups:
            grp.sort(key=lambda wd: wd['x'])
            text = " ".join(wd['text'] for wd in grp)
            alpha_r = sum(c.isalpha() for c in text) / max(1, len(text))
            if alpha_r < 0.25 and len(text) < 4:
                continue
            y_line   = min(wd['y'] for wd in grp)
            x_line   = min(wd['x'] for wd in grp)
            font_est = max(10, int(np.median([wd['h'] for wd in grp]) * 0.90))
            lines.append({
                "text":       text,
                "y_pct":      round(y_line / h, 4),
                "x_pct":      round(max(0.0, (x_line - 5) / w), 4),
                "fontsize_b": font_est,
            })

        # Ensure top → bottom order
        lines.sort(key=lambda l: l["y_pct"])
        return lines

    except Exception as e:
        return [{"error": str(e)}]
    finally:
        if Path(frame_path).exists():
            Path(frame_path).unlink(missing_ok=True)


def escape_dt(text):
    return (
        text
        .replace("\\", "\\\\")
        .replace("'",  "’")   # replace straight quote with curly (safe)
        .replace(":",  "\\:")
        .replace("%",  "\\%")
    )


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """Upload Video B → detect text lines (position + font size + OCR text)."""
    try:
        if "video_b" not in request.files:
            return jsonify({"error": "video_b manquant"}), 400

        vb     = request.files["video_b"]
        tmp_id = str(uuid.uuid4())
        tmp    = UPLOAD_DIR / tmp_id
        tmp.mkdir(parents=True, exist_ok=True)
        path_b = str(tmp / "b.mp4")
        vb.save(path_b)

        lines = analyze_text_lines(path_b)
        _, hb = get_video_dims(path_b)
        return jsonify({"lines": lines, "video_b_height": hb})

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
        scale  = ha / hb   # font size scaling B → A

        # Lines come as JSON from the frontend
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines = json.loads(lines_json)
        except Exception:
            lines = []

        if not lines:
            return jsonify({"error": "Aucune ligne de texte détectée ou fournie."}), 400

        # Build drawtext filter chain
        filters = []
        for line in lines:
            text      = line.get("text", "").strip()
            if not text:
                continue
            y_pct     = float(line.get("y_pct",     0.71))
            x_pct     = float(line.get("x_pct",     0.04))
            fontsize_b = int(line.get("fontsize_b", 32))

            x         = max(0, int(wa * x_pct))
            y         = max(0, int(ha * y_pct))
            fontsize  = max(10, int(fontsize_b * scale))

            filters.append(
                f"drawtext=fontfile={FONT}"
                f":text='{escape_dt(text)}'"
                f":fontsize={fontsize}"
                f":fontcolor=white"
                f":bordercolor=black:borderw=3"
                f":x={x}:y={y}"
            )

        if not filters:
            return jsonify({"error": "Aucun texte valide à incruster."}), 400

        vf = ",".join(filters)

        cmd = [
            "ffmpeg", "-y",
            "-i", path_a, "-i", path_b,
            "-filter_complex", f"[0:v]{vf}[out]",
            "-map", "[out]", "-map", "1:a",
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
