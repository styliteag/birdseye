# Changelog

All notable changes to birdseye will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
-

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
