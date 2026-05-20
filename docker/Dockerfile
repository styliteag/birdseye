# syntax=docker/dockerfile:1.7

# Builder: resolve and install Python deps with uv into a project-local venv.
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Runtime: slim image + tini + supervisor + cron + envsubst.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        supervisor \
        cron \
        gettext-base \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ARG VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="birdseye" \
      org.opencontainers.image.description="Bird's-eye view of a self-hosted NetBird: audit-event forwarder + scheduled cleanup jobs" \
      org.opencontainers.image.source="https://github.com/styliteag/birdseye" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="MIT"

ENV NETBIRD_TOOLKIT_VERSION=${VERSION} \
    NETBIRD_TOOLKIT_REVISION=${VCS_REF}

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# Root-level scripts (events.py, cleanup_ephemeral.py, allow_ping.py, ...).
COPY *.py /app/
# The forwarder lives under docker/.
COPY docker/event_forwarder.py /app/event_forwarder.py
# Runtime scaffolding.
COPY docker/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/crontab.template /etc/crontab.template
COPY docker/cron_wrapper.sh /app/cron_wrapper.sh
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /app/cron_wrapper.sh \
    && mkdir -p /var/lib/birdseye /var/log/supervisor

VOLUME ["/var/lib/birdseye"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
