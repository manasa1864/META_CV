# One deployable: the built frontend at /, the API under /api.
#   docker build -t meta-cv .
#   docker run -p 8000:8000 --env-file backend/.env -e JUDGE_MODE=1 meta-cv
# Secrets come from the host's environment variables only; nothing reads ~/.meta_cv/.env here.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    META_NO_HOME_ENV=1 \
    FRONTEND_DIST=/app/frontend/dist \
    DB_PATH=/data/meta.db
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY bench_data/ /app/bench_data/
COPY results/ /app/results/
COPY replays/ /app/replays/
COPY data/ /app/data/
COPY --from=web /web/dist /app/frontend/dist
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
