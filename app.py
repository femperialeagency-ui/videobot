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

LUMA = "0.299*r(X,Y)+0.587*g(X,Y)+0.114*b(X,Y)"


# ── Global JSON error handler ─────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Fichier trop grand (max 200 MB)"}), 413


# ── Helpers ───────────────────────────────────────────────────────

def get_video_dims(path):
    """Return (width, height) of first video stream."""
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


def detect_text_region(video_path: str) -> dict:
    """
    Detect the bounding box of text regions in a video frame.
    Uses white-pixel density analysis on the full frame.
    Returns {"y_start": fraction, "y_end": fraction} or {}.
    """
    frame_path = f"/tmp/detect_{uuid.uuid4().hex}.png"
    try:
        import numpy as np
        from PIL import Image

        subprocess.run(
            ["ffmpeg", "-ss", "1", "-i", video_path,
             "-vframes", "1", "-y", frame_path],
            capture_output=True, timeout=15
        )
        if not Path(frame_path).exists():
            return {}

        img = Image.open(frame_path).convert("RGB")
        arr = np.array(img)
        h, w = arr.shape[:2]

        # White pixels across the full frame
        white = (arr[:,:,0] > 170) & (arr[:,:,1] > 170) & (arr[:,:,2] > 170)
        row_sums = white.sum(axis=1)
        sig_rows = np.where(row_sums > w * 0.03)[0]  # >3% of width

        if len(sig_rows) < 5:
            return {}

        # Group into blocks (gap > 2.5% of height = new block)
        blocks, current = [], [sig_rows[0]]
        gap_thr = int(h * 0.025)
        for i in range(1, len(sig_rows)):
            if sig_rows[i] - sig_rows[i-1] <= gap_thr:
                current.append(sig_rows[i])
            else:
                if len(current) >= 3:
                    blocks.append(current)
                current = [sig_rows[i]]
        if len(current) >= 3:
            blocks.append(current)

        if not blocks:
            return {}

        pad = int(h * 0.01)
        y_start = max(0, blocks[0][0] - pad)
        y_end   = min(h, blocks[-1][-1] + pad)

        return {
            "y_start": float(y_start / h),
            "y_end":   float(y_end   / h),
        }

    except Exception as e:
        return {"error": str(e)}
    finally:
        if Path(frame_path).exists():
            Path(frame_path).unlink(missing_ok=True)


def build_pixel_overlay_filter(wb, hb, wa, ha, y_s, y_e):
    """
    Build ffmpeg filter_complex that copies the text pixels from Video B
    (detected region y_s..y_e) onto Video A using luma-based transparency.
    """
    region_h  = y_e - y_s
    overlay_y = int(ha * y_s)
    text_h_a  = max(1, int(ha * region_h))

    alpha = f"if(gt({LUMA},175),255,if(lt({LUMA},70),255,0))"

    return (
        f"[1:v]crop=iw:ih*{region_h:.5f}:0:ih*{y_s:.5f}[crop_b];"
        f"[crop_b]scale={wa}:{text_h_a}[scaled_b];"
        f"[scaled_b]format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'[text_layer];"
        f"[0:v][text_layer]overlay=x=0:y={overlay_y}[out]"
    )


def escape_dt(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("%", "\\%")


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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

        # ── Mode 1 : pixel copy (default) ─────────────────────────
        region = detect_text_region(path_b)

        if region and "y_start" in region:
            # Auto copy text pixels from B onto A
            filter_complex = build_pixel_overlay_filter(
                wb, hb, wa, ha,
                region["y_start"], region["y_end"]
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", path_a, "-i", path_b,
                "-filter_complex", filter_complex,
                "-map", "[out]", "-map", "1:a",
                "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-c:a", "aac", "-b:a", "128k",
                "-loglevel", "error",
                path_out
            ]

        else:
            # ── Mode 2 : drawtext fallback (manual text) ──────────
            line1     = request.form.get("line1", "").strip()
            line2     = request.form.get("line2", "").strip()
            fontsize  = max(10, min(120, int(request.form.get("fontsize",  32))))
            fontcolor = request.form.get("fontcolor", "white")
            borderw   = max(0,   min(10,  int(request.form.get("borderw",   3))))
            x_pct     = max(0.0, min(0.9,  float(request.form.get("x_pct",  0.04))))
            y_pct     = max(0.0, min(0.95, float(request.form.get("y_pct",  0.71))))
            gap_pct   = max(0.02, min(0.15, float(request.form.get("line_gap_pct", 0.055))))

            if not line1 and not line2:
                return jsonify({"error": "Aucun texte détecté dans la Vidéo B et aucun texte saisi manuellement."}), 400

            x  = max(0, int(wa * x_pct))
            y1 = max(0, int(ha * y_pct))
            y2 = y1 + int(ha * gap_pct)

            def dt(text, y):
                return (
                    f"drawtext=fontfile={FONT}:text='{escape_dt(text)}'"
                    f":fontsize={fontsize}:fontcolor={fontcolor}"
                    f":bordercolor=black:borderw={borderw}:x={x}:y={y}"
                )

            filters = []
            if line1: filters.append(dt(line1, y1))
            if line2: filters.append(dt(line2, y2))
            vf = ",".join(filters) if filters else "null"

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

        mode = "pixel_copy" if (region and "y_start" in region) else "drawtext"
        return jsonify({"job_id": job_id, "mode": mode})

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
