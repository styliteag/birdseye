#!/bin/bash
set -euo pipefail

# Capture the env vars cron jobs need into a file the wrapper sources.
# Debian cron strips inherited env, and cron.d files cannot hold secrets
# (they're parsed verbatim), so the wrapper sources this file instead.
{
  for var in \
    NB_URL NB_API_KEY NB_ADMIN_API_KEY TZ \
    SMTP_HOST SMTP_PORT SMTP_STARTTLS SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_TO \
    BACKUP_PATHS BACKUP_EMAIL_TO BACKUP_ZIP_PASSWORD \
    BACKUP_MAX_ATTACHMENT_MB BACKUP_LABEL; do
    if [ -n "${!var:-}" ]; then
      printf '%s=%q\n' "$var" "${!var}"
    fi
  done
} > /app/cron.env
chmod 600 /app/cron.env

CRON_FILE=/etc/cron.d/netbird
rm -f "$CRON_FILE"

cleanup_enabled=0
backup_enabled=0

if [ -n "${CRON_CLEANUP_EPHEMERAL:-}" ] && [ -n "${NB_ADMIN_API_KEY:-}" ]; then
  cleanup_enabled=1
elif [ -n "${CRON_CLEANUP_EPHEMERAL:-}" ]; then
  echo "[entrypoint] CRON_CLEANUP_EPHEMERAL set but NB_ADMIN_API_KEY empty — cleanup cron disabled" >&2
fi

if [ -n "${CRON_BACKUP_NETBIRD:-}" ] \
   && [ -n "${BACKUP_PATHS:-}" ] \
   && [ -n "${BACKUP_ZIP_PASSWORD:-}" ] \
   && [ -n "${SMTP_HOST:-}" ] \
   && [ -n "${SMTP_FROM:-}" ] \
   && { [ -n "${BACKUP_EMAIL_TO:-}" ] || [ -n "${SMTP_TO:-}" ]; }; then
  backup_enabled=1
elif [ -n "${CRON_BACKUP_NETBIRD:-}" ]; then
  echo "[entrypoint] CRON_BACKUP_NETBIRD set but BACKUP_PATHS / BACKUP_ZIP_PASSWORD / SMTP_* incomplete — backup cron disabled" >&2
fi

if [ "$cleanup_enabled" -eq 1 ] || [ "$backup_enabled" -eq 1 ]; then
  {
    echo "SHELL=/bin/bash"
    echo "PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    echo
    if [ "$cleanup_enabled" -eq 1 ]; then
      echo "${CRON_CLEANUP_EPHEMERAL} root /app/cron_wrapper.sh /app/.venv/bin/python /app/cleanup_ephemeral.py >> /proc/1/fd/1 2>> /proc/1/fd/2"
    fi
    if [ "$backup_enabled" -eq 1 ]; then
      echo "${CRON_BACKUP_NETBIRD} root /app/cron_wrapper.sh /app/.venv/bin/python /app/backup_volumes.py >> /proc/1/fd/1 2>> /proc/1/fd/2"
    fi
  } > "$CRON_FILE"
  # cron.d files must end with a newline and be 0644 root:root.
  printf '\n' >> "$CRON_FILE"
  chmod 0644 "$CRON_FILE"
  [ "$cleanup_enabled" -eq 1 ] && echo "[entrypoint] cron cleanup: $CRON_CLEANUP_EPHEMERAL" >&2
  [ "$backup_enabled" -eq 1 ] && echo "[entrypoint] cron backup:  $CRON_BACKUP_NETBIRD" >&2
else
  echo "[entrypoint] no cron jobs enabled" >&2
fi

exec "$@"
