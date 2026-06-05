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
    Detect individual text lines in a video frame.
    Returns list of {text, y_pct, x_pct, fontsize_b}.
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

        # Detect white pixels across full frame
        white = (arr[:,:,0] > 175) & (arr[:,:,1] > 175) & (arr[:,:,2] > 175)
        row_sums = white.sum(axis=1)
        sig_rows = np.where(row_sums > w * 0.03)[0]
        if len(sig_rows) == 0:
            return []

        # Group into individual visual lines (tight gap = 1.2% of height)
        blocks, current = [], [sig_rows[0]]
        for i in range(1, len(sig_rows)):
            if sig_rows[i] - sig_rows[i-1] <= int(h * 0.012):
                current.append(sig_rows[i])
            else:
                if len(current) >= 2:
                    blocks.append(current)
                current = [sig_rows[i]]
        if len(current) >= 2:
            blocks.append(current)

        lines = []
        for block in blocks:
            y1 = max(0, block[0] - 4)
            y2 = min(h, block[-1] + 4)
            block_h = y2 - y1

            if block_h < 20:   # skip thin artifacts
                continue

            # Font size estimate: ~75% of visual line height
            font_px = max(12, int(block_h * 0.75))

            # X: leftmost white column
            crop_arr = arr[y1:y2, :]
            white_mask = (
                (crop_arr[:,:,0] > 175) &
                (crop_arr[:,:,1] > 175) &
                (crop_arr[:,:,2] > 175)
            )
            col_sums = white_mask.sum(axis=0)
            x_cols   = np.where(col_sums > 0)[0]
            x_pct    = float(x_cols[0] / w) if len(x_cols) else 0.04

            # OCR: invert (white text→black on white bg — Tesseract's sweet spot)
            inv = np.full((block_h, w), 255, dtype=np.uint8)
            inv[white_mask] = 0
            pil_inv = Image.fromarray(inv)
            big = pil_inv.resize((w * 4, block_h * 4), Image.NEAREST)
            text = pytesseract.image_to_string(
                big, config="--psm 7 --oem 3"
            ).strip()
            text_clean = " ".join(text.split())

            # Discard lines with <30% alphabetic characters (artifacts)
            alpha_ratio = (
                sum(c.isalpha() for c in text_clean) / max(1, len(text_clean))
            )
            if alpha_ratio < 0.30 and len(text_clean) < 5:
                continue

            lines.append({
                "text":      text_clean,
                "y_pct":     round(y1 / h, 4),
                "x_pct":     round(x_pct, 4),
                "fontsize_b": font_px,
            })

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
