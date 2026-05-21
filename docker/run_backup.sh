#!/bin/bash
# Combined weekly NetBird backup: runs the volume snapshot (if mounted) and
# the API config export (if an admin token is configured). Each step is
# independent — a failure in one does not block the other, since the two
# tools mail their own error notifications. Sourced env comes from
# /app/cron.env via cron_wrapper.sh.

set +e

ran_any=0

if [ -n "${BACKUP_PATHS:-}" ]; then
  echo "[run_backup] starting volume backup" >&2
  /app/.venv/bin/python /app/backup_volumes.py
  echo "[run_backup] volume backup exited rc=$?" >&2
  ran_any=1
fi

if [ -n "${NB_ADMIN_API_KEY:-}" ]; then
  echo "[run_backup] starting API export" >&2
  /app/.venv/bin/python /app/export_objects.py
  echo "[run_backup] API export exited rc=$?" >&2
  ran_any=1
fi

if [ "$ran_any" -eq 0 ]; then
  echo "[run_backup] neither BACKUP_PATHS nor NB_ADMIN_API_KEY set — nothing to do" >&2
  exit 1
fi
