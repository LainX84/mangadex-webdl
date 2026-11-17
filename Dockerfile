# syntax=docker/dockerfile:1
# One image for the web app, worker and scheduler (different commands).

# ---------------------------------------------------------------- frontend build
FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MDM_STATIC_DIR=/app/static \
    MDM_DOWNLOADS_ROOT=/downloads \
    MDM_DATA_DIR=/data

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY --from=frontend /src/dist ./static

# `mdm` admin CLI (create-admin, reset-password, init-library, ...)
RUN printf '#!/bin/sh\nexec python -m app.cli "$@"\n' > /usr/local/bin/mdm \
 && chmod 0755 /usr/local/bin/mdm \
 && mkdir -p /data /downloads \
 && chmod 0777 /data \
 && python -m compileall -q app

# Runs as an unprivileged user; override the UID/GID with `user:` in compose so
# it matches the owner of the NFS share.
USER 1000:1000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD ["mdm", "health", "api"]

# Forwarded headers are handled by the app's own trusted-proxy middleware.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "10"]
