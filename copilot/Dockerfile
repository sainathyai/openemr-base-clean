# Clinical Co-Pilot serving layer — Path-B public demo image.
# Runs the FastAPI app + UI from captured fixtures (no OpenEMR, no DB), with the
# deterministic stubs (no Claude spend). One small container is the whole deploy.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# OCR engine for the scanned/faxed-document path (Week 2). The binary lives in
# the image so OCR works identically in local Docker and on the cloud box; no
# host install. tesseract-ocr-eng ships the English language data.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY server ./server

# Demo defaults (override at runtime to point at a live OpenEMR or enable Claude):
#   COPILOT_FIXTURES  - serve the captured 20-patient snapshot, no EMR needed
#   COPILOT_FORCE_STUB - deterministic narration, zero API spend
ENV COPILOT_FIXTURES=/app/app/data/fixtures \
    COPILOT_FORCE_STUB=1 \
    COPILOT_HOST=0.0.0.0 \
    COPILOT_PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
