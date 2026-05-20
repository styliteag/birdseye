#!/bin/bash
set -euo pipefail

# Capture the subset of env vars cron jobs need into a file the wrapper sources.
# Avoids leaking secrets into /etc/cron.d/, which cron parses verbatim.
{
  for var in NB_URL NB_API_KEY NB_ADMIN_API_KEY TZ; do
    if [ -n "${!var:-}" ]; then
      printf '%s=%q\n' "$var" "${!var}"
    fi
  done
} > /app/cron.env
chmod 600 /app/cron.env

# Render /etc/cron.d/netbird from the template only if both schedule and the
# admin token are present; otherwise the cron job would fail noisily every tick.
if [ -n "${CRON_CLEANUP_EPHEMERAL:-}" ] && [ -n "${NB_ADMIN_API_KEY:-}" ]; then
  envsubst < /etc/crontab.template > /etc/cron.d/netbird
  # cron.d files must end with a newline and be 0644 root:root.
  printf '\n' >> /etc/cron.d/netbird
  chmod 0644 /etc/cron.d/netbird
  echo "[entrypoint] cron schedule: $CRON_CLEANUP_EPHEMERAL" >&2
elif [ -n "${CRON_CLEANUP_EPHEMERAL:-}" ]; then
  rm -f /etc/cron.d/netbird
  echo "[entrypoint] CRON_CLEANUP_EPHEMERAL set but NB_ADMIN_API_KEY empty — cron disabled" >&2
else
  rm -f /etc/cron.d/netbird
  echo "[entrypoint] CRON_CLEANUP_EPHEMERAL is empty — cron jobs disabled" >&2
fi

exec "$@"
