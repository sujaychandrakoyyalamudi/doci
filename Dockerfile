FROM node:22-bookworm-slim AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS backend
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
COPY backend/ ./backend/
RUN uv sync --frozen --all-extras --no-dev
COPY --from=frontend /web/dist ./backend/doci/static
RUN useradd --create-home --uid 10001 doci && mkdir -p /app/.data && chown -R doci:doci /app/.data
USER doci
ENV PORT=8080 DATA_DIR=/tmp/doci
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn doci.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
