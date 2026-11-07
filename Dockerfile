# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN uv sync --locked --no-dev

FROM python:3.13-slim AS runtime

ARG WORKTRACK_UID=10001
ARG WORKTRACK_GID=10001

RUN groupadd --gid "${WORKTRACK_GID}" worktrack \
    && useradd --uid "${WORKTRACK_UID}" --gid "${WORKTRACK_GID}" \
       --home-dir /var/lib/worktrack --create-home --shell /usr/sbin/nologin worktrack

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME="/var/lib/worktrack/config" \
    XDG_DATA_HOME="/var/lib/worktrack/data" \
    XDG_STATE_HOME="/var/lib/worktrack/state" \
    XDG_RUNTIME_DIR="/var/lib/worktrack/run" \
    WORKTRACK_METRICS_HOST="0.0.0.0" \
    WORKTRACK_METRICS_PORT="9112"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=worktrack:worktrack src ./src

USER worktrack

EXPOSE 9112

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = os.environ.get('WORKTRACK_METRICS_PORT', '9112'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).read()"]

CMD ["python", "-m", "worktrack.telemetry", "--serve"]
