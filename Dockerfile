# syntax=docker/dockerfile:1

# --- build stage: compile wheels so the runtime image needs no toolchain ---
FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# --- runtime stage ---
FROM python:3.12-slim

# Never run as root: the container mounts nothing it needs to write to.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=app:app resumescreener/ ./resumescreener/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/
COPY --chown=app:app wsgi.py ./

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JSON_LOGS=true \
    APP_ENV=production

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# Screening calls block on the Claude API for many seconds, so the workers are
# thread-backed and the timeout is generous. gthread beats sync here because the
# work is I/O bound, not CPU bound.
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--threads", "8", \
     "--worker-class", "gthread", \
     "--timeout", "180", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
