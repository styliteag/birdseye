# Changelog

All notable changes to birdseye will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
