#!/bin/bash
# Source the container environment captured at startup, then exec the cron job.
# Debian cron strips inherited env, so we replay it from /app/cron.env.
set -e
if [ -f /app/cron.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /app/cron.env
  set +a
fi
exec "$@"
