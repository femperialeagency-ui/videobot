# ── Base image ────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System deps ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-liberation \
      tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# ── App setup ─────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Runtime ───────────────────────────────────────────────────────
ENV PORT=10000
EXPOSE 10000

CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "0", \
     "--keep-alive", "65", \
     "--log-level", "info"]
