# Python services (biz-api, biz-worker). Build from the repo root:
#   docker build -f deploy/Dockerfile -t biz-os .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# All workspace members must be present for a locked sync. The cache mount keeps
# downloaded wheels between builds without hiding dependency or packaging errors.
COPY pyproject.toml uv.lock ./
COPY kernel kernel
COPY ports ports
COPY capabilities capabilities
COPY packets packets
COPY roles roles
COPY industries industries
COPY apps apps
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-dev --all-packages

FROM python:3.12-slim
# WeasyPrint loads these shared libraries at runtime. A font is needed for PDFs
# with readable text on the otherwise minimal image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 biz
WORKDIR /app
COPY --from=build --chown=biz:biz /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER biz
EXPOSE 8000
# biz-api: uvicorn. biz-worker overrides the command in docker-compose.yml.
CMD ["uvicorn", "biz_kernel.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
