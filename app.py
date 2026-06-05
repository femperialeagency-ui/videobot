import os
import uuid
import json
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
UPLOAD_DIR = Path("/tmp/videobot_jobs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


def probe_video(path):
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, timeout=30
    )
    for s in json.loads(r.stdout).get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return {}


def escape_dt(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("%", "\\%")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    if "video_a" not in request.files or "video_b" not in request.files:
        return jsonify({"error": "Les deux videos sont requises."}), 400

    va = request.files["video_a"]
    vb = request.files["video_b"]
    line1       = request.form.get("line1", "").strip()
    line2       = request.form.get("line2", "").strip()
    fontsize    = max(10, min(120, int(request.form.get("fontsize", 32))))
    fontcolor   = request.form.get("fontcolor", "white")
    borderw     = max(0, min(10,  int(request.form.get("borderw", 3))))
    x_pct       = max(0.0, min(0.9,  float(request.form.get("x_pct", 0.04))))
    y_pct       = max(0.0, min(0.95, float(request.form.get("y_pct", 0.71))))
    gap_pct     = max(0.02, min(0.15, float(request.form.get("line_gap_pct", 0.055))))

    job_id  = str(uuid.uuid4())
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    path_a   = str(job_dir / "a.mp4")
    path_b   = str(job_dir / "b.mp4")
    path_out = str(job_dir / "c.mp4")

    va.save(path_a)
    vb.save(path_b)

    info = probe_video(path_a)
    w = int(info.get("width",  576))
    h = int(info.get("height", 1024))
    x  = max(0, int(w * x_pct))
    y1 = max(0, int(h * y_pct))
    y2 = y1 + int(h * gap_pct)

    def dt(text, y):
        return (f"drawtext=fontfile={FONT}:text='{escape_dt(text)}'"
                f":fontsize={fontsize}:fontcolor={fontcolor}"
                f":bordercolor=black:borderw={borderw}:x={x}:y={y}")

    filters = []
    if line1: filters.append(dt(line1, y1))
    if line2: filters.append(dt(line2, y2))
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
        return jsonify({"error": "Timeout : traitement trop long (>3 min)"}), 500

    if proc.returncode != 0 or not Path(path_out).exists():
        err = proc.stderr[-800:] if proc.stderr else "ffmpeg a echoue"
        return jsonify({"error": err}), 500

    return jsonify({"job_id": job_id})


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
