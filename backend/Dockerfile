# The API as one container (Render, or any host that runs Docker). Build from the repository root:
#   docker build -f backend/Dockerfile -t consult-assistant-api .
# The content/ folder is part of the image (the defaults); dashboard edits live in the database.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    EMBEDDING_CACHE_DIR=/app/.cache/fastembed

WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

# The embedding model goes into the image, so a restart or a cold start never downloads it again.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='$EMBEDDING_CACHE_DIR')"

COPY content /app/content
COPY backend/app ./app
COPY backend/scripts ./scripts

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

# One process: the server holds live calls and the job worker; 512 MB hosts have room for one.
# --proxy-headers: the host's proxy terminates HTTPS. Render sets PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 20"]
