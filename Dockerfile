# ── Base image ────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System deps: ffmpeg + Liberation fonts (Helvetica substitute) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── App setup ─────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Runtime ───────────────────────────────────────────────────────
ENV PORT=10000
EXPOSE 10000

# gunicorn with 1 worker + 4 threads (video processing is CPU-bound per job)
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "300", \
     "--log-level", "info"]
