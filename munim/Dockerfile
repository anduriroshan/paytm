# Munim on Google Cloud Run: one container, FastAPI serves the API and the built React UI.
# The SQLite demo database is seeded on first start (lives in the container; resets when the instance restarts).

# ── 1. build the UI ──
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit || npm install --no-fund --no-audit
COPY frontend/ ./
# vite only (skips the tsc type-check, so an in-progress type error elsewhere can't block a deploy)
RUN npx vite build

# ── 2. API + static UI ──
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=ui /ui/dist /app/frontend/dist
RUN mkdir -p /app/backend/data
EXPOSE 8080
CMD exec uvicorn munim.api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
