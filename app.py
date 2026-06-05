import os
import json
import uuid
import threading
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload
UPLOAD_DIR = Path("/tmp/videobot_jobs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

# In-memory job store  {job_id: {status, progress, error}}
JOBS: dict = {}


# ─────────────────────────── helpers ────────────────────────────

def probe_video(path: str) -> dict:
    """Return basic video stream info via ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", path],
        capture_output=True, text=True
    )
    data = json.loads(r.stdout)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return {}


def build_drawtext(txt_path: str, fontsize: int, color: str,
                   borderw: int, x: int, y: int) -> str:
    """Build a single drawtext filter string."""
    return (
        f"drawtext=fontfile='{FONT}'"
        f":textfile='{txt_path}'"
        f":fontsize={fontsize}"
        f":fontcolor={color}"
        f":bordercolor=black"
        f":borderw={borderw}"
        f":x={x}:y={y}"
    )


def run_job(job_id: str, path_a: str, path_b: str, path_out: str,
            line1: str, line2: str, fontsize: int,
            fontcolor: str, borderw: int,
            x_pct: float, y_pct: float, line_gap_pct: float):
    """ffmpeg processing – runs in a background thread."""
    try:
        JOBS[job_id]["progress"] = 5

        # Get video A dimensions for proportional placement
        info = probe_video(path_a)
        vid_w = int(info.get("width", 576))
        vid_h = int(info.get("height", 1024))

        x  = max(0, int(vid_w * x_pct))
        y1 = max(0, int(vid_h * y_pct))
        y2 = y1 + int(vid_h * line_gap_pct)

        # Write text to temp files (avoids shell-quoting nightmares)
        job_dir = Path(path_out).parent
        txt1 = str(job_dir / "line1.txt")
        txt2 = str(job_dir / "line2.txt")
        Path(txt1).write_text(line1, encoding="utf-8")
        Path(txt2).write_text(line2, encoding="utf-8")

        # Build video filter chain
        filters = []
        if line1.strip():
            filters.append(build_drawtext(txt1, fontsize, fontcolor, borderw, x, y1))
        if line2.strip():
            filters.append(build_drawtext(txt2, fontsize, fontcolor, borderw, x, y2))

        vf = ",".join(filters) if filters else "null"

        JOBS[job_id]["progress"] = 15

        cmd = [
            "ffmpeg",
            "-i", path_a,
            "-i", path_b,
            "-filter_complex", f"[0:v]{vf}[v]",
            "-map", "[v]",
            "-map", "1:a",
            "-shortest",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            path_out, "-y"
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True)

        if proc.returncode == 0:
            JOBS[job_id] = {"status": "done", "progress": 100}
        else:
            # Surface last 800 chars of stderr
            err = proc.stderr[-800:] if proc.stderr else "Unknown ffmpeg error"
            JOBS[job_id] = {"status": "error", "error": err, "progress": 0}

    except Exception as exc:
        JOBS[job_id] = {"status": "error", "error": str(exc), "progress": 0}


# ─────────────────────────── routes ─────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    if "video_a" not in request.files or "video_b" not in request.files:
        return jsonify({"error": "Les deux vidéos sont requises."}), 400

    video_a = request.files["video_a"]
    video_b = request.files["video_b"]

    line1       = request.form.get("line1", "").strip()
    line2       = request.form.get("line2", "").strip()
    fontsize    = max(10, min(120, int(request.form.get("fontsize", 32))))
    fontcolor   = request.form.get("fontcolor", "white")
    borderw     = max(0, min(10, int(request.form.get("borderw", 3))))
    x_pct       = max(0.0, min(0.9, float(request.form.get("x_pct", 0.04))))
    y_pct       = max(0.0, min(0.95, float(request.form.get("y_pct", 0.71))))
    line_gap_pct = max(0.02, min(0.15, float(request.form.get("line_gap_pct", 0.055))))

    job_id  = str(uuid.uuid4())
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    path_a   = str(job_dir / "video_a.mp4")
    path_b   = str(job_dir / "video_b.mp4")
    path_out = str(job_dir / "video_c.mp4")

    video_a.save(path_a)
    video_b.save(path_b)

    JOBS[job_id] = {"status": "processing", "progress": 0}

    t = threading.Thread(
        target=run_job,
        args=(job_id, path_a, path_b, path_out,
              line1, line2, fontsize, fontcolor, borderw,
              x_pct, y_pct, line_gap_pct),
        daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    path = UPLOAD_DIR / job_id / "video_c.mp4"
    if path.exists():
        return send_file(
            str(path),
            as_attachment=True,
            download_name="video_C.mp4",
            mimetype="video/mp4"
        )
    return jsonify({"error": "Fichier introuvable"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
