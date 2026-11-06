# Python services (biz-api, biz-worker). Build from the repo root:
#   docker build -f deploy/Dockerfile -t biz-os .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so this layer caches until a pyproject or the lock changes.
COPY pyproject.toml uv.lock ./
COPY kernel/pyproject.toml kernel/pyproject.toml
COPY ports/fakes/pyproject.toml ports/fakes/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --all-packages || true
COPY kernel kernel
COPY ports ports
COPY packets packets
COPY roles roles
COPY industries industries
COPY apps apps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 biz
WORKDIR /app
COPY --from=build --chown=biz:biz /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER biz
EXPOSE 8000
# biz-api: uvicorn. biz-worker overrides the command in docker-compose.yml.
CMD ["uvicorn", "biz_kernel.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
