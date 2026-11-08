FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY ingest ./ingest
# data/ is NOT baked into the image: it's mounted from the VM's disk (reference.duckdb + app.db)

# Fixed uid: DEPLOY.md has you `chown -R 1000:1000 data` on the VM so the mounted volume is writable.
RUN useradd --create-home --uid 1000 appuser && mkdir -p /app/data && chown -R appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fs http://localhost:8000/healthz || exit 1
# One worker on purpose: the reminder + retention loops run in-process, and SQLite wants a single writer.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
