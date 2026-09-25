#!/bin/bash
set -euo pipefail

# Capture the env vars cron jobs need into a file the wrapper sources.
# Debian cron strips inherited env, and cron.d files cannot hold secrets
# (they're parsed verbatim), so the wrapper sources this file instead.
{
  for var in \
    NB_URL NB_API_KEY NB_ADMIN_API_KEY TZ JOBS_DIR \
    SMTP_HOST SMTP_PORT SMTP_TLS_MODE SMTP_STARTTLS SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_TO \
    BACKUP_PATHS BACKUP_EMAIL_TO EXPORT_EMAIL_TO BACKUP_ZIP_PASSWORD \
    BACKUP_MAX_ATTACHMENT_MB BACKUP_LABEL BACKUP_EXCLUDE \
    CHECKMK_SPOOL_DIR \
    MAINTENANCE_POSTURE_CHECK MAINTENANCE_POSTURE_RULE MAINTENANCE_ALLOW_PING \
    MAINTENANCE_DRY_RUN MAINTENANCE_EMAIL_TO MAINTENANCE_SPOOL_MAX_AGE \
    MIRROR_URL MIRROR_API_KEY MIRROR_APPLY MIRROR_PRUNE MIRROR_SECTIONS \
    MIRROR_PROTECTED_GROUPS MIRROR_SNAPSHOT_DIR \
    CLONE_SSH_HOST CLONE_SSH_PORT CLONE_SSH_KEY CLONE_SSH_KNOWN_HOSTS CLONE_SSH_STRICT \
    CLONE_TARGETS CLONE_PRIMARY_HOST CLONE_STANDBY_IP CLONE_PAYLOAD_DIR CLONE_STAGE_DIR \
    CLONE_DB_PATHS CLONE_DB_SUBDIR CLONE_CONFIG_FILES CLONE_TARGET_PATHS CLONE_SHARED_PATHS \
    CLONE_ACME_SOURCE CLONE_ACME_REMOTE CLONE_INGRESS_ROOT CLONE_INGRESS_PROJECT \
    CLONE_INGRESS_COMPOSE CLONE_IMAGES CLONE_DOCKER_SOCKET CLONE_MIN_ROWS \
    CLONE_KEEP_SNAPSHOTS CLONE_FAILOVER_TARGET CLONE_EMAIL_TO \
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
REGISTRY=()  # one line per known job for jobrun.py (birdseye-web Jobs page)

# add_job <key> <schedule> <command> <label>. An empty schedule means "not configured".
# Every run goes through jobrun.py, which records start, end, exit code and a
# log tail under $JOBS_DIR for the web UI; output still reaches the container log.
add_job() {
  local key="$1" schedule="$2" command="$3" name="$4"
  [ -z "$schedule" ] && return 0
  JOBS+=("$schedule root /app/cron_wrapper.sh /app/.venv/bin/python /app/jobrun.py run $key -- $command >> /proc/1/fd/1 2>> /proc/1/fd/2")
  SUMMARY+=("$name: $schedule")
  REGISTRY+=("$(printf '%s\t%s\t%s\t%s\t' "$key" "$name" "$schedule" "$command")")
}

# job_off <key> <label> <reason> — known job, not scheduled; shown as disabled.
job_off() {
  REGISTRY+=("$(printf '%s\t%s\t\t\t%s' "$1" "$2" "$3")")
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
    add_job cleanup "$CRON_CLEANUP_EPHEMERAL" "/app/.venv/bin/python /app/cleanup_ephemeral.py" "cleanup"
  else
    echo "[entrypoint] CRON_CLEANUP_EPHEMERAL set but NB_ADMIN_API_KEY empty — cleanup cron disabled" >&2
    job_off cleanup "cleanup" "need: NB_ADMIN_API_KEY"
  fi
else
  job_off cleanup "cleanup" "CRON_CLEANUP_EPHEMERAL not set"
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
    add_job backup "$CRON_BACKUP_NETBIRD" "/app/run_backup.sh" "backup (${bits% })"
  else
    echo "[entrypoint] CRON_BACKUP_NETBIRD set but incomplete — backup cron disabled; need:$gaps" >&2
    job_off backup "backup" "need:$gaps"
  fi
else
  job_off backup "backup" "CRON_BACKUP_NETBIRD not set"
fi

# --- account mirror to a second controller -----------------------------------
# Without MIRROR_APPLY the scheduled run is a dry run: it reports the drift it
# would fix and writes nothing. Deliberate — this job can delete.
if [ -n "${CRON_MIRROR_ACCOUNT:-}" ]; then
  gaps=$(missing NB_URL NB_API_KEY MIRROR_URL MIRROR_API_KEY)
  if [ -z "$gaps" ]; then
    mode="dry-run"
    [ -n "${MIRROR_APPLY:-}" ] && mode="apply"
    add_job mirror "$CRON_MIRROR_ACCOUNT" "/app/.venv/bin/python /app/mirror_account.py" "mirror ($mode)"
  else
    echo "[entrypoint] CRON_MIRROR_ACCOUNT set but incomplete — mirror cron disabled; need:$gaps" >&2
    job_off mirror "mirror" "need:$gaps"
  fi
else
  job_off mirror "mirror" "CRON_MIRROR_ACCOUNT not set"
fi

# --- account maintenance: posture attachment, then ICMP companions -----------
# Needs at least one step configured. Posture runs before allow_ping, because
# the companions copy each policy's posture checks.
if [ -n "${CRON_NETBIRD_MAINTENANCE:-}" ]; then
  gaps=$(missing NB_URL NB_API_KEY)
  if [ -z "${MAINTENANCE_POSTURE_CHECK:-}${MAINTENANCE_ALLOW_PING:-}" ]; then
    gaps="$gaps MAINTENANCE_POSTURE_CHECK|MAINTENANCE_ALLOW_PING"
  fi
  if [ -z "$gaps" ]; then
    bits=""
    [ -n "${MAINTENANCE_POSTURE_CHECK:-}" ] && bits="${bits}posture "
    [ -n "${MAINTENANCE_ALLOW_PING:-}" ] && bits="${bits}allow-ping "
    [ -n "${MAINTENANCE_DRY_RUN:-}" ] && bits="${bits}(dry-run) "
    add_job maintenance "$CRON_NETBIRD_MAINTENANCE" \
      "/app/.venv/bin/python /app/netbird_maintenance.py" "maintenance (${bits% })"
  else
    echo "[entrypoint] CRON_NETBIRD_MAINTENANCE set but incomplete — maintenance cron disabled; need:$gaps" >&2
    job_off maintenance "maintenance" "need:$gaps"
  fi
else
  job_off maintenance "maintenance" "CRON_NETBIRD_MAINTENANCE not set"
fi

# --- standby clone: database + config to a failover host ---------------------
if [ -n "${CRON_CLONE_STANDBY:-}" ]; then
  gaps=$(missing CLONE_SSH_HOST CLONE_TARGETS CLONE_DB_PATHS NB_URL)
  if [ -z "$gaps" ]; then
    add_job clone-standby "$CRON_CLONE_STANDBY" "/app/.venv/bin/python /app/clone_standby.py run" "clone-standby"
  else
    echo "[entrypoint] CRON_CLONE_STANDBY set but incomplete — clone cron disabled; need:$gaps" >&2
    job_off clone-standby "clone-standby" "need:$gaps"
  fi
else
  job_off clone-standby "clone-standby" "CRON_CLONE_STANDBY not set"
fi

# --- offsite config archive over ssh -----------------------------------------
if [ -n "${CRON_BACKUP_OFFSITE:-}" ]; then
  gaps=$(missing OFFSITE_SSH_HOST OFFSITE_REMOTE_DIR OFFSITE_PATHS)
  if [ -z "$gaps" ]; then
    add_job backup-offsite "$CRON_BACKUP_OFFSITE" "/app/.venv/bin/python /app/backup_offsite.py" "backup-offsite"
  else
    echo "[entrypoint] CRON_BACKUP_OFFSITE set but incomplete — offsite cron disabled; need:$gaps" >&2
    job_off backup-offsite "backup-offsite" "need:$gaps"
  fi
else
  job_off backup-offsite "backup-offsite" "CRON_BACKUP_OFFSITE not set"
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

# --- job registry + request directory for birdseye-web ------------------------
# The web container runs as uid 10001 and only ever creates request files:
# requests/ is write-and-enter for everyone but not listable (1733), state/
# stays read-only for it. jobrun.py serve (supervisord) validates each request
# against this registry and JOB_TRIGGERS before it starts anything.
JOBS_DIR="${JOBS_DIR:-/var/lib/birdseye/jobs}"
export JOBS_DIR
install -d -m 0755 "$JOBS_DIR" "$JOBS_DIR/state"
install -d -m 1733 "$JOBS_DIR/requests"
printf '%s\n' "${REGISTRY[@]}" | /app/.venv/bin/python /app/jobrun.py register \
  || echo "[entrypoint] could not write job registry — Jobs page will be empty" >&2

exec "$@"
