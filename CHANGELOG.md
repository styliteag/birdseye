# Changelog

All notable changes to birdseye will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
-

## [0.2.0] - 2026-05-21

### Added
- `EMAIL_STARTUP_TEST=true` and `MATTERMOST_STARTUP_TEST=true` — one-shot
  smoke probes that fire at container start, before the poll loop. The
  email probe sends a self-describing message (host, time, transport,
  recipients) over the resolved SMTP transport; the Mattermost probe
  posts a single canned message to the webhook. Failure is logged but
  never aborts the forwarder — the probes are diagnostic, not gating.
- `SMTP_TLS_MODE=starttls|tls|none` — explicit SMTP transport selector
  for the forwarder and both backup jobs. `tls` enables implicit TLS
  (SMTPS) on port 465; `starttls` keeps the previous submission
  behaviour on port 587; `none` is plain SMTP on port 25.
  `SMTP_PORT` is now derived from the mode when left empty.
- `BACKLOG_WARN_THRESHOLD` (default 1000) — one-shot WARN log when a
  single audit-events poll returns more than this many events. The
  NetBird audit endpoint has no cursor parameter, so every poll
  re-downloads the full list; this flags the situation before it
  becomes a measurable latency problem.

### Deprecated
- `SMTP_STARTTLS=true|false` — still honoured as a fallback when
  `SMTP_TLS_MODE` is unset (true → starttls, false → none), but new
  deployments should set `SMTP_TLS_MODE` directly.

### Changed
- `SmtpConfig` (frozen dataclass in `smtp_helpers.py`) replaces the
  ad-hoc `dict[str, object]` that `backup_common.smtp_config()` used
  to return. The forwarder's email sink gains a parallel
  `EmailSinkConfig(mode, smtp, digest_seconds)` where `smtp=None`
  cleanly represents "disabled" instead of carrying empty strings.
  Six `# type: ignore[arg-type]` markers and five redundant
  `str(...)` / `int(...)` casts removed across `backup_common`,
  `backup_volumes`, `export_objects`, and `event_forwarder`.
- `nb_client.py` — shared NetBird `APIClient` builder. Every operator
  script (events, list_policies, cleanup_ephemeral, allow_ping,
  manage_posture, netbird_overview, setup_keys, export_objects,
  event_forwarder) used to carry its own copy of `_client_from_env`
  and `_host_from_url`. Picks the right token via `key="user"|"admin"`,
  with an explicit `fallback_to_user` option for `setup_keys.py`.
- Forwarder outage tracking moved from `time.monotonic()` to
  `time.time()`. monotonic resets across process restarts, which
  invalidated the persisted-state work below.

### Fixed
- `cleanup_ephemeral.py` now reports `NB_ADMIN_API_KEY must be set`
  when the admin token is missing. Previously it read
  `NB_ADMIN_API_KEY` but the error message named `NB_API_KEY`.
- README `docker exec` examples now invoke `/app/.venv/bin/python`
  instead of `uv run`. `uv` is only present in the builder stage of
  the Docker image, so the previous examples failed at runtime.
- Forwarder `outage_started` and `outage_alerted` are now persisted
  to the state file. A container restart during a NetBird API outage
  previously caused a duplicate `🚨 API unreachable` Mattermost alert
  on every reboot; the alert now fires at most once per outage.
- `MattermostSink.send_events` no longer drops `batch_notice` when the
  POST fails. The skipped-events warning is preserved across retries
  until Mattermost actually acknowledges it.

## [0.1.5] - 2026-05-21

### Added
- `export_objects.py` — second mail in the same weekly backup cron. Pulls
  every NetBird configuration endpoint (peers, groups, policies, users,
  setup-keys, routes, dns, posture-checks, networks, accounts) via the
  admin API into one JSON file each plus a `manifest.json`, packs that
  into a separate AES256-encrypted 7z (reuses `BACKUP_ZIP_PASSWORD`),
  and sends it as a second SMTP attachment. Endpoint 404s are skipped
  best-effort and recorded in the manifest. Recipient defaults to
  `EXPORT_EMAIL_TO`, falling back to `BACKUP_EMAIL_TO` then `SMTP_TO`.
- `backup_common.py` — shared SMTP / 7z helpers used by both the volume
  backup and the API export.
- `docker/run_backup.sh` — wrapper invoked by the backup cron; runs
  whichever of the two jobs is configured, with independent error
  handling so a failure in one does not block the other.
- `BACKUP_EXCLUDE` — comma-separated 7z wildcards stripped from the
  volume archive (case-insensitive, recursive). Useful for large
  derived files (GeoIP DBs, caches) that need not be mailed weekly.

### Changed
- The `CRON_BACKUP_NETBIRD` cron now drives both jobs (volume snapshot
  + API export) via `run_backup.sh`. Either job can be disabled by
  leaving its inputs empty (`BACKUP_PATHS` for volumes,
  `NB_ADMIN_API_KEY` for the API export).

## [0.1.4] - 2026-05-21

### Added
- `backup_volumes.py` — optional weekly NetBird volume backup. Mount the
  NetBird Docker volumes read-only into the birdseye container, set
  `BACKUP_PATHS`, `BACKUP_ZIP_PASSWORD`, and `CRON_BACKUP_NETBIRD`
  (typical `0 3 * * 0`), and the existing SMTP sink configuration is
  reused to deliver a password-protected 7z archive (AES256 with
  encrypted filenames) as a mail attachment. The size limit
  (`BACKUP_MAX_ATTACHMENT_MB`, default 20) is compared against the
  base64-encoded SMTP payload (≈1.4× the raw archive), matching what
  Gmail/Exchange actually count; oversize archives trigger a `— FAILED`
  notification mail instead so the operator notices before the next
  run. SQLite hot-backup caveat is documented in the README.
- Image: `p7zip-full` added to the runtime stage.

### Changed
- `docker/entrypoint.sh` now renders `/etc/cron.d/netbird` inline
  instead of via `envsubst` on a template, so `cleanup_ephemeral` and
  the new `backup_volumes` job enable independently. Removed the
  unused `docker/crontab.template` and the `gettext-base` apt
  dependency.
- `MATTERMOST_USERNAME` default renamed from `NetBird` to `birdseye` (the
  webhook bot now identifies as the forwarder, not the source system).
  Override via env if you want to keep the old display name.
- `docker/event_forwarder.py` Mattermost rendering rewritten for readability.
  Each event now renders as a verb-led one-liner:
  ```
  `2026-05-21 10:58:43`  **Peer login expired**: chuckcybermac.local · `10.48.231.168` · Nuremberg, DE  _Andre Keller_
  `2026-05-21 00:02:58`  **Group updated**: "Bensheim-User" → "Bensheim-Users"  _Wim Bonis_
  `2026-05-21 11:21:07`  **User deleted**: Bonis (bonis@bonis.de)  _Wim Bonis_
  ```
  - Per-activity-code verb phrase ("Peer login expired", "Group updated",
    "User deleted", …) replaces the raw `activity_code` plus duplicate
    `activity` prose.
  - Shape-aware subject formatters: peers show `name · IP · city, country`;
    groups show the rename arrow `"old" → "new"`; users show
    `username (email)`; account/setting events drop the opaque target id
    entirely.
  - Low-signal meta keys (`fqdn`, `created_at`, `issued`,
    `location_connection_*`, `location_geo_name_id`) dropped from the
    Mattermost output; consumed keys never reappear in the trailing meta
    dump. Stdout and email keep the full meta for log fidelity.
  - The "system" initiator is suppressed (automatic events no longer trail
    a noisy `_system_`).

### Fixed
- `docker/event_forwarder.py` Mattermost rendering: wrap colon-bearing meta
  values (IPv6 addresses, ISO timestamps) in inline code so Mattermost's
  emoji parser stops turning `:a:` inside `2003:a:172b:…` into the regional
  indicator A.

## [0.1.3] - 2026-05-20

### Added
- Shared `resolver` module mapping NetBird audit-event initiator IDs to
  human-readable labels, used by both `events.py` and the forwarder.
- `docker/event_forwarder.py` now resolves setup-key and service-user
  initiators in stdout, Mattermost, and email output — events from
  setup-key joins show `setup-key:<name>` instead of `<system>` /
  `_system_`, and email subjects use the resolved label.

### Changed
- `docker/event_forwarder.py` default `POLL_INTERVAL` raised from `30`
  to `60` (matching `events.py`). Halves the volume of NetBird-server
  `failed to resolve user info` WARNs caused by `GET /events/audit`
  returning the full history on every poll (no server-side filter, no
  conditional-GET on that endpoint).

## [0.1.2] - 2026-05-20

### Added
- `events.py` resolves setup-key and service-user initiators in the
  formatted output column (was `<system>` for anything without a human
  initiator name).

### Changed
- `events.py` default `--interval` raised from `5` to `60` seconds to
  reduce the WARN burst triggered by each `GET /events/audit` call.

## [0.1.1] - 2026-05-20

### Added
-

## [0.1.0] - 2026-05-20

### Added
- Toolkit Docker image bundling `event_forwarder.py` and the existing one-shot
  scripts (`cleanup_ephemeral.py`, `allow_ping.py`, `manage_posture.py`, ...).
- Long-running audit-event forwarder with three sinks: stdout, Mattermost
  webhook, and SMTP email (off / immediate / digest modes).
- Per-sink fnmatch filters via `STDOUT_INCLUDE`, `MATTERMOST_INCLUDE`,
  `EMAIL_INCLUDE`.
- `last_id` persistence on a named volume with `MAX_CATCHUP` cap and
  seed-from-latest on first boot.
- Supervisor-managed cron for `cleanup_ephemeral.py`, schedule overridable via
  `CRON_CLEANUP_EPHEMERAL` env var.
- Mattermost self-alert when the NetBird API is unreachable for longer than
  `OUTAGE_ALERT_MINUTES`.
