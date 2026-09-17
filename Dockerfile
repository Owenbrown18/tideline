# One image, two processes: `sitewatch worker` (default) or the API (M2).
# Multi-stage: the build stage has uv and compiles the virtualenv; the final
# stage copies only that virtualenv, so no build tools ship to production.
# Builds natively for arm64 (Graviton, Apple Silicon) and amd64.

# ---- build ------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first, in their own layer: this layer is reused on every build
# where only application code changed, which makes rebuilds seconds, not minutes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
RUN groupadd --system --gid 10001 sitewatch \
 && useradd --system --uid 10001 --gid sitewatch --home-dir /app --shell /usr/sbin/nologin sitewatch
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SITEWATCH_ALEMBIC_INI=/app/alembic.ini
# Never run as root inside the container.
USER sitewatch
CMD ["sitewatch", "worker"]
