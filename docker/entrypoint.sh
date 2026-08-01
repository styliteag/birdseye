#!/bin/bash
set -euo pipefail

# Capture the env vars cron jobs need into a file the wrapper sources.
# Debian cron strips inherited env, and cron.d files cannot hold secrets
# (they're parsed verbatim), so the wrapper sources this file instead.
{
  for var in \
    NB_URL NB_API_KEY NB_ADMIN_API_KEY TZ \
    SMTP_HOST SMTP_PORT SMTP_TLS_MODE SMTP_STARTTLS SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_TO \
    BACKUP_PATHS BACKUP_EMAIL_TO EXPORT_EMAIL_TO BACKUP_ZIP_PASSWORD \
    BACKUP_MAX_ATTACHMENT_MB BACKUP_LABEL BACKUP_EXCLUDE \
    CHECKMK_SPOOL_DIR \
    MIRROR_URL MIRROR_API_KEY MIRROR_APPLY MIRROR_PRUNE MIRROR_SECTIONS \
    MIRROR_PROTECTED_GROUPS MIRROR_SNAPSHOT_DIR \
    CLONE_SSH_HOST CLONE_SSH_PORT CLONE_SSH_KEY CLONE_SSH_KNOWN_HOSTS CLONE_SSH_STRICT \
    CLONE_TARGETS CLONE_PRIMARY_HOST CLONE_STANDBY_IP CLONE_PAYLOAD_DIR CLONE_STAGE_DIR \
    CLONE_DB_PATHS CLONE_DB_SUBDIR CLONE_CONFIG_FILES CLONE_TARGET_PATHS CLONE_SHARED_PATHS \
    CLONE_ACME_SOURCE CLONE_ACME_REMOTE CLONE_INGRESS_ROOT CLONE_INGRESS_PROJECT \
    CLONE_INGRESS_COMPOSE CLONE_IMAGES CLONE_DOCKER_SOCKET CLONE_MIN_ROWS \
    CLONE_KEEP_SNAPSHOTS CLONE_FAILOVER_TARGET \
    OFFSITE_SSH_HOST OFFSITE_SSH_PORT OFFSITE_SSH_KEY OFFSITE_SSH_KNOWN_HOSTS OFFSITE_SSH_STRICT \
    OFFSITE_REMOTE_DIR OFFSITE_PATHS OFFSITE_DB_PATHS OFFSITE_BASE_DIR OFFSITE_EXCLUDE \
    OFFSITE_PREFIX OFFSITE_KEEP OFFSITE_WORK_DIR OFFSITE_EMAIL_TO; do
    if [ -n "${!var:-}" ]; then
      printf '%s=%q\n' "$var" "${!var}"
    fi
  done
  # The standby clone's per-target settings are named after CLONE_TARGETS, so
  # they cannot be listed above — pass through everything matching the pattern.
  # The second grep drops the fixed CLONE_* settings that happen to end in one
  # of those words (CLONE_SSH_HOST, CLONE_INGRESS_ROOT, …); they are already
  # written above. A target named "ssh", "primary" or "ingress" would collide.
  for var in $(compgen -A variable \
               | grep -E '^CLONE_[A-Z0-9_]+_(ROOT|PROJECT|COMPOSE|HOST|AUTOSTART|CERT_COPY|DB_DIR)$' \
               | grep -vE '^CLONE_(SSH|PRIMARY|INGRESS)_' \
               || true); do
    if [ -n "${!var:-}" ]; then
      printf '%s=%q\n' "$var" "${!var}"
    fi
  done
} > /app/cron.env
chmod 600 /app/cron.env

CRON_FILE=/etc/cron.d/netbird
rm -f "$CRON_FILE"

JOBS=()      # cron lines to install
SUMMARY=()   # one "name: schedule" line per enabled job, logged at startup

# add_job <schedule> <command> <name>. An empty schedule means "not configured".
add_job() {
  local schedule="$1" command="$2" name="$3"
  [ -z "$schedule" ] && return 0
  JOBS+=("$schedule root /app/cron_wrapper.sh $command >> /proc/1/fd/1 2>> /proc/1/fd/2")
  SUMMARY+=("$name: $schedule")
}

# missing <VAR>… — print the names of the listed env vars that are empty.
missing() {
  local out=""
  for var in "$@"; do
    [ -z "${!var:-}" ] && out="$out $var"
  done
  echo "${out# }"
}

# --- ephemeral peer cleanup --------------------------------------------------
if [ -n "${CRON_CLEANUP_EPHEMERAL:-}" ]; then
  if [ -n "${NB_ADMIN_API_KEY:-}" ]; then
    add_job "$CRON_CLEANUP_EPHEMERAL" "/app/.venv/bin/python /app/cleanup_ephemeral.py" "cleanup"
  else
    echo "[entrypoint] CRON_CLEANUP_EPHEMERAL set but NB_ADMIN_API_KEY empty — cleanup cron disabled" >&2
  fi
fi

# --- weekly backup: volume snapshot + API export, both mailed ----------------
# Needs SMTP plus the shared archive password, and at least one source: either
# BACKUP_PATHS (volume snapshot) or NB_ADMIN_API_KEY (API export). If both are
# set, both run sequentially.
if [ -n "${CRON_BACKUP_NETBIRD:-}" ]; then
  gaps=$(missing BACKUP_ZIP_PASSWORD SMTP_HOST SMTP_FROM)
  if [ -z "${BACKUP_EMAIL_TO:-}${EXPORT_EMAIL_TO:-}${SMTP_TO:-}" ]; then
    gaps="$gaps BACKUP_EMAIL_TO|EXPORT_EMAIL_TO|SMTP_TO"
  fi
  if [ -z "${BACKUP_PATHS:-}${NB_ADMIN_API_KEY:-}" ]; then
    gaps="$gaps BACKUP_PATHS|NB_ADMIN_API_KEY"
  fi
  if [ -z "$gaps" ]; then
    bits=""
    [ -n "${BACKUP_PATHS:-}" ] && bits="${bits}volumes "
    [ -n "${NB_ADMIN_API_KEY:-}" ] && bits="${bits}api-export "
    add_job "$CRON_BACKUP_NETBIRD" "/app/run_backup.sh" "backup (${bits% })"
  else
    echo "[entrypoint] CRON_BACKUP_NETBIRD set but incomplete — backup cron disabled; need:$gaps" >&2
  fi
fi

# --- account mirror to a second controller -----------------------------------
# Without MIRROR_APPLY the scheduled run is a dry run: it reports the drift it
# would fix and writes nothing. Deliberate — this job can delete.
if [ -n "${CRON_MIRROR_ACCOUNT:-}" ]; then
  gaps=$(missing NB_URL NB_API_KEY MIRROR_URL MIRROR_API_KEY)
  if [ -z "$gaps" ]; then
    mode="dry-run"
    [ -n "${MIRROR_APPLY:-}" ] && mode="apply"
    add_job "$CRON_MIRROR_ACCOUNT" "/app/.venv/bin/python /app/mirror_account.py" "mirror ($mode)"
  else
    echo "[entrypoint] CRON_MIRROR_ACCOUNT set but incomplete — mirror cron disabled; need:$gaps" >&2
  fi
fi

# --- standby clone: database + config to a failover host ---------------------
if [ -n "${CRON_CLONE_STANDBY:-}" ]; then
  gaps=$(missing CLONE_SSH_HOST CLONE_TARGETS CLONE_DB_PATHS NB_URL)
  if [ -z "$gaps" ]; then
    add_job "$CRON_CLONE_STANDBY" "/app/.venv/bin/python /app/clone_standby.py run" "clone-standby"
  else
    echo "[entrypoint] CRON_CLONE_STANDBY set but incomplete — clone cron disabled; need:$gaps" >&2
  fi
fi

# --- offsite config archive over ssh -----------------------------------------
if [ -n "${CRON_BACKUP_OFFSITE:-}" ]; then
  gaps=$(missing OFFSITE_SSH_HOST OFFSITE_REMOTE_DIR OFFSITE_PATHS)
  if [ -z "$gaps" ]; then
    add_job "$CRON_BACKUP_OFFSITE" "/app/.venv/bin/python /app/backup_offsite.py" "backup-offsite"
  else
    echo "[entrypoint] CRON_BACKUP_OFFSITE set but incomplete — offsite cron disabled; need:$gaps" >&2
  fi
fi

if [ ${#JOBS[@]} -gt 0 ]; then
  {
    echo "SHELL=/bin/bash"
    echo "PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    echo
    printf '%s\n' "${JOBS[@]}"
  } > "$CRON_FILE"
  # cron.d files must end with a newline and be 0644 root:root.
  printf '\n' >> "$CRON_FILE"
  chmod 0644 "$CRON_FILE"
  for line in "${SUMMARY[@]}"; do
    echo "[entrypoint] cron $line" >&2
  done
else
  echo "[entrypoint] no cron jobs enabled" >&2
fi

exec "$@"
