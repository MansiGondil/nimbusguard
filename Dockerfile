# syntax=docker/dockerfile:1

# ---- Build the React dashboard ----
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Python runtime (API + worker share this image) ----
FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY --from=web /web/dist ./static
RUN useradd --system --uid 10001 nimbus && mkdir -p /data && chown nimbus /data
USER nimbus
ENV NG_DATABASE_URL=sqlite+aiosqlite:////data/nimbusguard.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
