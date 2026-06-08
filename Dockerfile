# syntax=docker/dockerfile:1

# Multi-stage build: compile the React UI, then serve it together with the
# FastAPI agent from one Python image. A single container runs the whole app —
# ideal for a one-command local run and for a free cloud Space.

# --- Stage 1: build the React chat UI ---
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
# Baked into the bundle so the same-origin UI can authenticate to the API. On a
# public Space, pass --build-arg VITE_API_KEY=<secret> matching APP_API_KEY.
ARG VITE_API_KEY=change-me
ENV VITE_API_KEY=$VITE_API_KEY
RUN npm run build

# --- Stage 2: Python runtime serving the API + the built UI ---
FROM python:3.11-slim AS app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
COPY --from=web /web/dist ./web/dist

# Build the synthetic ledger at image-build time (deterministic, ~45s). The
# .dockerignore excludes any local *.duckdb so the image always builds it fresh.
RUN python data/generate.py

EXPOSE 8000
# $PORT lets a Space override the port (e.g. 7860); defaults to 8000 locally.
CMD ["sh", "-c", "uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
