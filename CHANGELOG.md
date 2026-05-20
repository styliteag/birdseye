# Changelog

All notable changes to birdseye will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
-

## [0.1.2] - 2026-05-20

### Added
-

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
