# ── Base image ────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System deps ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-liberation \
      fonts-noto-color-emoji \
      tesseract-ocr \
      tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

# ── App setup ─────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Runtime ───────────────────────────────────────────────────────
ENV PORT=10000
EXPOSE 10000

# serve.py : sans PRODUCTION_WORKER_ENABLED=1, remplacé par EXACTEMENT la commande d'avant
#   gunicorn app:app --bind 0.0.0.0:10000 --workers 1 --threads 4 --timeout 0 --keep-alive 65 --log-level info
# avec PRODUCTION_WORKER_ENABLED=1 : Gunicorn + worker de production FEMPERIALE (processus séparés).
CMD ["python", "serve.py"]
