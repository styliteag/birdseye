# syntax=docker/dockerfile:1.7
#
# birdseye-web: group/policy editor and access matrix for a self-hosted NetBird.
# Build from the repo root:  docker build -f docker/web/Dockerfile -t birdseye-web .

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin birdseye

ARG VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="birdseye-web" \
      org.opencontainers.image.description="Group/policy editor and access matrix for a self-hosted NetBird" \
      org.opencontainers.image.source="https://github.com/styliteag/birdseye" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY birdseye_web /app/birdseye_web

USER birdseye
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status != 200)"

# Sessions live in process memory: keep a single worker, or users bounce
# between workers that do not know their session.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "--factory", "birdseye_web.app:build", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--no-server-header", "--proxy-headers"]
