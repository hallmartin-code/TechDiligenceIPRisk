# Production image for Railway (or any container host).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TDIP_LOG_LEVEL=INFO

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY templates ./templates

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv
USER appuser

EXPOSE 8040

# One worker: analysis jobs are tracked in process memory. Railway injects $PORT.
CMD uvicorn app.web.server:app --host 0.0.0.0 --port ${PORT:-8040} --workers 1 --proxy-headers --forwarded-allow-ips="*"
