# syntax=docker/dockerfile:1.7
# Enzo: backend + built web app in one image. Build from the repository root:
#
#   docker build -f backend/Dockerfile -t enzo .

# ── 1. Build the web app (frontend/) ─────────────────────────
FROM node:20-alpine AS web
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── 2. Backend ───────────────────────────────────────────────
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 568 enzo

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Enzo's browser (agent/browser.py, ENABLE_BROWSER=true) needs Chromium: build with --build-arg INSTALL_BROWSER=true
# (~400 MB more). Off by default.
ARG INSTALL_BROWSER=false
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN if [ "$INSTALL_BROWSER" = "true" ]; then python -m playwright install --with-deps chromium && chmod -R a+rX /ms-playwright; fi

COPY backend/ .
COPY --from=web /src/dist /app/web
RUN chmod +x /app/docker-entrypoint.sh && mkdir -p /app/data && chown -R enzo:enzo /app

ENV FRONTEND_DIST=/app/web \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python scripts/healthcheck.py

# Starts as root only to fix ownership of the mounted ./data, then drops to user "enzo".
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "server.py"]
