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
FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


# ── Global JSON error handler ─────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Fichier trop grand (max 200 MB)"}), 413


# ── Helpers ───────────────────────────────────────────────────────

def probe_video(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(r.stdout) if r.stdout.strip() else {}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return s
    except Exception:
        pass
    return {}


def detect_text_from_video(video_path: str) -> dict:
    """
    Extract text content and position from a video using OCR.
    Returns dict with line1, line2, x_pct, y_pct, gap_pct.
    """
    frame_path = f"/tmp/ocr_{uuid.uuid4().hex}.png"
    try:
        import numpy as np
        import pytesseract
        from PIL import Image

        # Extract one frame at ~1 second
        subprocess.run(
            ["ffmpeg", "-ss", "1", "-i", video_path,
             "-vframes", "1", "-y", frame_path],
            capture_output=True, timeout=15
        )
        if not Path(frame_path).exists():
            return {}

        img  = Image.open(frame_path).convert("RGB")
        arr  = np.array(img)
        h, w = arr.shape[:2]

        # ── Position detection via white-pixel analysis ───────────
        # Text is typically in the bottom 50% of the frame
        roi_y = int(h * 0.5)
        roi   = arr[roi_y:]

        # Pixels that are very bright (white text fill)
        white = (roi[:,:,0] > 180) & (roi[:,:,1] > 180) & (roi[:,:,2] > 180)
        row_sums = white.sum(axis=1)

        # Rows with enough white pixels to be text (>4% of width)
        sig_rows = np.where(row_sums > w * 0.04)[0]
        if len(sig_rows) < 3:
            return {}

        abs_first = roi_y + int(sig_rows[0])
        abs_last  = roi_y + int(sig_rows[-1])
        y1_pct    = abs_first / h

        # Detect line break: gap > 2.5% of frame height
        gap_pct = 0.055
        y2_pct  = y1_pct + gap_pct
        for i in range(1, len(sig_rows)):
            if sig_rows[i] - sig_rows[i-1] > h * 0.025:
                abs_line2 = roi_y + int(sig_rows[i])
                y2_pct    = abs_line2 / h
                gap_pct   = float(y2_pct - y1_pct)
                break

        # X: leftmost white-pixel column in the text region
        text_slice = white[sig_rows[0]:sig_rows[-1]+1, :]
        col_sums   = text_slice.sum(axis=0)
        text_cols  = np.where(col_sums > 1)[0]
        x_pct = max(0.01, min(0.15, float(text_cols[0] / w))) if len(text_cols) else 0.04

        # ── OCR ──────────────────────────────────────────────────
        pad  = int(h * 0.015)
        crop = img.crop((0, max(0, abs_first - pad), w, min(h, abs_last + pad)))
        c_arr = np.array(crop)

        # Binary mask: white pixels → white, rest → black
        mask = (c_arr[:,:,0] > 180) & (c_arr[:,:,1] > 180) & (c_arr[:,:,2] > 180)
        pil_mask = Image.fromarray((mask * 255).astype(np.uint8))

        # Upscale 3× for better Tesseract accuracy
        big = pil_mask.resize(
            (pil_mask.width * 3, pil_mask.height * 3),
            Image.NEAREST
        )

        raw   = pytesseract.image_to_string(big, config="--psm 6").strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]

        return {
            "line1":   lines[0] if lines else "",
            "line2":   lines[1] if len(lines) > 1 else "",
            "x_pct":   x_pct,
            "y_pct":   float(y1_pct),
            "y2_pct":  float(y2_pct),
            "gap_pct": gap_pct,
        }

    except Exception as e:
        return {"ocr_error": str(e)}
    finally:
        if Path(frame_path).exists():
            Path(frame_path).unlink(missing_ok=True)


def escape_dt(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("%", "\\%")


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """Analyze Video B and return detected text + position."""
    try:
        if "video_b" not in request.files:
            return jsonify({"error": "video_b manquant"}), 400

        vb     = request.files["video_b"]
        job_id = str(uuid.uuid4())
        tmp    = UPLOAD_DIR / job_id
        tmp.mkdir(parents=True, exist_ok=True)
        path_b = str(tmp / "b.mp4")
        vb.save(path_b)

        result = detect_text_from_video(path_b)
        return jsonify(result)

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

        # ── Text: use provided values OR auto-detect from Video B ──
        line1     = request.form.get("line1", "").strip()
        line2     = request.form.get("line2", "").strip()
        fontsize  = max(10, min(120, int(request.form.get("fontsize",  32))))
        fontcolor = request.form.get("fontcolor", "white")
        borderw   = max(0,   min(10,  int(request.form.get("borderw",   3))))
        x_pct     = max(0.0, min(0.9,  float(request.form.get("x_pct",  0.04))))
        y_pct     = max(0.0, min(0.95, float(request.form.get("y_pct",  0.71))))
        gap_pct   = max(0.02, min(0.15, float(request.form.get("line_gap_pct", 0.055))))

        # Auto-detect if text fields are empty
        auto_used = False
        if not line1 and not line2:
            detected = detect_text_from_video(path_b)
            if detected and "line1" in detected:
                line1     = detected.get("line1", "")
                line2     = detected.get("line2", "")
                x_pct     = detected.get("x_pct",   x_pct)
                y_pct     = detected.get("y_pct",   y_pct)
                gap_pct   = detected.get("gap_pct", gap_pct)
                auto_used = True

        # ── Video dimensions for proportional placement ────────────
        info = probe_video(path_a)
        w = int(info.get("width",  576))
        h = int(info.get("height", 1024))
        x  = max(0, int(w * x_pct))
        y1 = max(0, int(h * y_pct))
        y2 = y1 + int(h * gap_pct)

        # ── Build ffmpeg filter ───────────────────────────────────
        filters = []
        if line1:
            filters.append(
                f"drawtext=fontfile={FONT}:text='{escape_dt(line1)}'"
                f":fontsize={fontsize}:fontcolor={fontcolor}"
                f":bordercolor=black:borderw={borderw}:x={x}:y={y1}"
            )
        if line2:
            filters.append(
                f"drawtext=fontfile={FONT}:text='{escape_dt(line2)}'"
                f":fontsize={fontsize}:fontcolor={fontcolor}"
                f":bordercolor=black:borderw={borderw}:x={x}:y={y2}"
            )
        vf = ",".join(filters) if filters else "null"

        cmd = [
            "ffmpeg", "-y",
            "-i", path_a, "-i", path_b,
            "-filter_complex", f"[0:v]{vf}[v]",
            "-map", "[v]", "-map", "1:a",
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

        resp = {"job_id": job_id}
        if auto_used:
            resp["detected"] = {"line1": line1, "line2": line2}
        return jsonify(resp)

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
