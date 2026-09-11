FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY agent4good ./agent4good
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN groupadd --gid 10001 agent4good && useradd --uid 10001 --gid agent4good --no-create-home agent4good
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY scripts ./scripts
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 A4G_DATA_DIR=/app/data
RUN mkdir -p /app/data && chown agent4good:agent4good /app/data
USER agent4good
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)"
CMD ["uvicorn", "agent4good.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
