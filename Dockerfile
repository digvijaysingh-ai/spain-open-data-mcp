# Hosted server image: the package plus its data files, so a new instance answers without
# downloading anything. Build: docker build -t spain-open-data-mcp .
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev && useradd --system --uid 10001 app

# Data files: OpenStreetMap snapshot, census tract shapes, walking graphs (checked by sha256).
FROM base AS data
ENV SPAIN_MCP_CACHE_DIR=/data
RUN uv run --no-sync spain-open-data-mcp prefetch && rm -f /data/http.sqlite

FROM base
COPY --from=data --chown=app /data /data
ENV SPAIN_MCP_CACHE_DIR=/data \
    SPAIN_MCP_HTTP_CACHE_MB=64 \
    HOST=0.0.0.0 \
    PORT=8080 \
    LOG_LEVEL=INFO
USER app
EXPOSE 8080
CMD ["/app/.venv/bin/spain-open-data-mcp", "http"]
