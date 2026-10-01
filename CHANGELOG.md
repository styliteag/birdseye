# Changelog

All notable changes to birdseye will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
-

## [0.9.1] - 2026-10-01

### Added
- birdseye-web footer with the versions in use: birdseye-web (and its git
  revision), the birdseye container (from the shared jobs directory, when
  mounted) and NetBird's management server, with a hint when NetBird has an
  update (`GET /api/instance/version`, cached 10 minutes).

### Changed
- Config history snapshots follow changes faster: a save in birdseye-web
  requests one right away (seconds; `history` is now in the default
  `JOB_TRIGGERS`), the forwarder fires on time instead of at its next poll,
  and `HISTORY_SETTLE_SECONDS` defaults to 15 instead of 60 – changes made
  elsewhere show up after about 15–75 s. The History notice polls every 5 s.
- birdseye-web shows times in the viewer's own time zone (audit log, config
  history, snapshot notice, setup key expiry, Jobs page) instead of UTC. The
  UTC time stays as tooltip and as the text without JavaScript.

## [0.9.0] - 2026-10-01

### Added
- birdseye-web **peers page** (`/peers`): search and filter by online state,
  user, group, OS and expired login, sorted by last seen. Peer editor: name,
  SSH, login expiration and approval (`PUT /peers/{id}` from a fresh GET),
  group membership (each changed group re-read and PUT whole, resources
  kept) with the live access preview, delete with a preview of the lost
  access, and the peer's effective access in and out. Peers link to their
  editor from the matrix, reachability, the cell popover, the user editor and
  the anomalies page.
- **Config history**: `config_history.py` (cron `CRON_CONFIG_HISTORY`,
  retention `HISTORY_KEEP_DAYS`, default 90) writes a dated JSON snapshot of
  the configuration into the shared jobs directory. birdseye-web's
  **History** page (owners and admins) compares two snapshots per object
  type, shows each object's versions, and restores a single policy or group
  (`PUT`, or `POST` when it was deleted) after an access preview; a policy
  whose groups or posture checks are gone is not restored.
- birdseye-web **resources list** (`/resources`): all network resources with
  address kind and size (subnets flagged), network and router state,
  resource groups, the policies that reach them and how many peers can
  reach them; filters for unreachable and wider-than-one-host resources.
- History page: changes are listed field by field (`rules › web › ports:
  + 8443`, old → new, IDs as names) instead of raw JSON, and a notice shows
  when the configuration changed after the newest snapshot (or a snapshot
  is running), reloading the page once the new snapshot is there.
- Config history follows changes: the audit-event forwarder starts a
  snapshot ~1 min after the last configuration change (`HISTORY_ON_CHANGE`,
  `HISTORY_SETTLE_SECONDS`, `HISTORY_TRIGGER_INCLUDE/EXCLUDE`), and a run
  that finds the configuration unchanged writes nothing (`--force` to
  override). The birdseye image now also contains `birdseye_web/`, so the
  job and the History page compare snapshots the same way.
- birdseye-web **audit log** (`/audit`): NetBird's audit events with
  filters (initiator, activity or category, target, date range, free text),
  paging, names instead of IDs (deleted objects by their recorded name) and
  links to the editors. *History* links on the group, policy, user, peer,
  setup key, network and resource editors open the log filtered to that
  object. Changes made through birdseye-web are marked (kept in memory
  since the last restart).
- Anomalies: housekeeping checks – expired peer login, peer waiting for
  approval, stale peer (`WEB_STALE_PEER_DAYS`, default 30), outdated client
  (`WEB_MIN_CLIENT_VERSION`, or the newest version seen), setup keys without
  expiry / reusable without limit / never used / expired but not revoked,
  users without a device, unused posture checks. Each links to the peer,
  setup key or user editor.
- birdseye-web **networks editor** (`/networks`): networks with their
  resources and routers; create, rename and delete networks
  (`/networks`), resources (`/networks/{id}/resources`, with access preview
  and lost-access preview on delete) and routers (`/networks/{id}/routers`:
  peer or peer groups, metric, masquerade, enabled). `/resources/{id}`
  links to a resource from anywhere. Anomalies “Resource without an active
  router” and “Resource no policy reaches” link to the router and resource
  editors.
- birdseye-web **setup keys page** (`/setup-keys`): state badges (valid,
  expired, revoked, exhausted), create with expiry, usage limit, auto-groups
  and ephemeral flag, edit auto-groups, revoke (`PUT /setup-keys/{id}` from a
  fresh GET), delete revoked keys. The new key's value appears once, in the
  create response page only; it is not logged, cached, or put in a URL.
  Hidden for roles that may not manage setup keys. Bulk *Revoke selected* /
  *Delete selected* from the list (each key re-read and written on its own;
  delete skips keys that are not revoked).

### Changed
- `export_objects.py`: endpoint list and dump helpers are public
  (`ENDPOINTS`, `dump_objects`, `write_manifest`) so `config_history.py`
  reuses them.
- Anomalies: the severity counters respect `WEB_ANOMALY_IGNORE` like the list.

## [0.8.1] - 2026-10-01

### Added
- birdseye-web **user editor** (`/users`): change a user's role, block state
  and auto-assigned groups (`PUT /users/{id}`, IdP-issued groups carried
  over), with the live access preview. Shows each of the user's devices whose
  groups differ from the auto-groups and fixes selected ones by updating the
  affected groups (`PUT /groups/{id}`, re-read after the user update so
  NetBird's group propagation is respected, resources kept). Owner role cannot
  be given; your own role and block state are left alone.

### Changed
- Anomalies: “Device groups differ from the user's default” now links to the
  user editor. The check and the editor share one drift module.
- Anomalies: more findings link to the editor that fixes them. A peer only
  in “All” opens its user's editor (or a new group with the peer preselected
  when it has no user); a resource no policy reaches opens a new policy with
  that resource as destination (`/groups/new?peer=…`,
  `/policies/new?dst_resource=…`).
- Matrix, Group × Group: **Show unused groups** option. Lists every group as
  row and column, also those no rule uses yet (respects “Hide All” and the
  row/column filters), so you can click an empty cell to give them access.

## [0.8.0] - 2026-09-28

### Added
- birdseye-web group editor: assign **users** as well as devices. Selecting a
  user adds the group to their auto-assigned groups (`PUT /users/{id}`), so
  their devices join it — existing devices too when the account has group
  propagation on, which the note and the live preview take into account.
  Needs `users` update permission; hidden for IdP- and integration-issued
  groups. Service users are never changed.

## [0.7.0] - 2026-09-28

### Removed
- **Account mirror** (`mirror_account.py`, `CRON_MIRROR_ACCOUNT`, all
  `MIRROR_*` settings). It could not serve as a failover target (peers cannot
  be created through the API), deleted on the target by default, and
  `clone_standby.py` covers failover. A leftover `CRON_MIRROR_ACCOUNT` is
  ignored with a warning in the container log; remove it and the `MIRROR_*`
  lines from your `.env`.

## [0.6.0] - 2026-09-25

### Added
- **Jobs page** in birdseye-web: status of every birdseye cron job (schedule,
  last run, exit code, duration, 20-run history, log tail) and of the
  audit-event forwarder (heartbeat, last event, API errors). NetBird owners and
  admins can start `cleanup` and `maintenance` early, also as a dry run.
  Setup: [docs/web-ui.md](docs/web-ui.md#jobs-page-optional).
- `jobrun.py` in the birdseye image: every cron job now runs through it and
  leaves a status file under `/var/lib/birdseye/jobs`; a supervisord program
  (`job_runner`) starts jobs requested by the web UI — only jobs that are
  enabled and listed in the new `JOB_TRIGGERS` (default `cleanup,maintenance`).
  Two runs of the same job never overlap any more; the second is skipped.
- The forwarder writes a heartbeat (`jobs/state/forwarder.json`).

## [0.5.0] - 2026-09-23

### Added
- **birdseye-web**, a second image (`styliteag/birdseye-web`): a web UI that
  shows who can reach what and edits groups and policies. Setup:
  [docs/web-ui.md](docs/web-ui.md).
  - Sign-in with your own NetBird account through the embedded IdP
    (`netbird-dashboard` client, authorization code + PKCE). Every API call
    carries your token, so NetBird enforces your role and its audit log names
    you. Requires registering the callback URL in `dashboardRedirectURIs`.
  - Access matrix in five views (Group × Group, Peer × Peer, Group × Resource,
    Peer × Resource, User × Destination) with name/protocol/port filters and a
    per-cell explanation of which policy grants the access.
  - Edit from the matrix: revoke (remove a group from a rule, disable, delete)
    or grant access from a cell; every change previews the peer pairs that gain
    or lose access before it is confirmed.
  - Group editor (create, rename, membership) and multi-rule policy editor
    (`netbird-ssh`, port ranges, posture checks, resource/peer destinations),
    both with a live gained/lost preview. Policy writes use raw dicts, so the
    SDK's protocol enum is not in the way; SSH `authorized_groups` survive edits.
  - Reachability query ("can X reach Y on tcp/22?") with the reasons.
  - Anomalies page: rules targeting single peers or resources, devices whose
    groups drifted from their user's auto-groups, peers inside resource groups,
    empty or missing groups in policies, resources without router or policy,
    peers only in All, unused groups, disabled policies.
    `WEB_ANOMALY_IGNORE` skips groups by name (e.g. documentation groups).
- The release workflow builds and publishes both images (amd64 + arm64).
- `.dockerignore`, so a local `.env` never enters a build context.
- `pytest` suite for the web UI; `release.sh` runs it before tagging.

## [0.4.1] - 2026-08-10

### Added
-

### Fixed
- `allow_ping.py` and `manage_posture.py` fought each other once an hour over the
  ICMP companion of any `POSTURE_IGNORE` policy. The companion inherits the
  original's empty `source_posture_checks`, but `manage_posture` reads only the
  *companion's own* description — saw no marker, attached the posture check;
  `allow_ping` then saw the drift and stripped it again. Two `policy.update`
  events per hour, forever, and between the two steps the companion was
  geo-gated — the precise thing the marker on the original exists to prevent
  (ACME renewal must not depend on geolocation). Companion descriptions now
  carry `POSTURE_IGNORE` over from their original, and the policy description is
  part of the drift signature, so companions that never got the marker are
  repaired instead of silently churning. Observed on
  `ZPING: Sty-ACME-Clients-Access`.

## [0.4.0] - 2026-08-01

### Fixed
- `clone_standby.py` never mailed. A failed refresh set the Checkmk check CRIT
  and wrote the container log, but sent nothing — while `backup_offsite.py` did
  both, and the documentation claimed the clone did too. With
  `CHECKMK_SPOOL_DIR` unset that left a failing standby visible only in
  `docker logs`, which is exactly the silent rot the job is supposed to prevent.
  Adds `CLONE_EMAIL_TO` (falling back to `BACKUP_EMAIL_TO`, then `SMTP_TO`) and
  mails on both paths: a failing step, and a configuration error that stops the
  run before the first step.

### Changed
- `backup_common.notify_failure()` — one failure-mail helper for every
  scheduled job, replacing a copy per job. Three copies is how the clone ended
  up with none. `error_mail()`'s body is now generic ("… failed") instead of
  "did not produce a deliverable archive", which was wrong for the two jobs
  that produce no archive.

### Added
-

## [0.3.0] - 2026-08-01

### Added
- `netbird_maintenance.py` + `CRON_NETBIRD_MAINTENANCE` — runs the account
  reconcilers on a schedule instead of by hand: `manage_posture.py --all
  --add-posture $MAINTENANCE_POSTURE_CHECK`, then `allow_ping.py` when
  `MAINTENANCE_ALLOW_PING` is set. Posture runs first because the ICMP
  companions copy each policy's `source_posture_checks`, so the other order
  leaves them a cycle behind. The steps are independent, both honour
  `MAINTENANCE_DRY_RUN`, failures mail `MAINTENANCE_EMAIL_TO`, and each step's
  own summary line lands in the `NetBird_Account_Maintenance` Checkmk check —
  so a reconciler that starts changing things on every run is visible rather
  than buried in a container log. Policies keep their existing escape hatches:
  `POSTURE_IGNORE` / `PING_IGNORE` in the description.

## [0.2.2] - 2026-08-01

### Added
- `mirror_account.py` — mirrors one account's configuration onto a second
  controller over the API (`MIRROR_URL` / `MIRROR_API_KEY`), matching
  objects by name so it is idempotent and re-runnable. Dry run by
  default; the scheduled run (`CRON_MIRROR_ACCOUNT`) writes only with
  `MIRROR_APPLY=true`. The source is opened through a client that rejects
  every non-GET method, and the run aborts if both URLs resolve to the
  same host. Peers cannot be created through the API, so this is
  explicitly not a failover path.
- `clone_standby.py` — clones the *database* to a standby host over ssh
  (`CRON_CLONE_STANDBY`), which makes failover a DNS change. Live SQLite
  stores are copied with the online backup API (consistent, no downtime,
  source read-only), checksummed into a payload together with the config
  files, rsynced, and installed by a generated `install.sh` that verifies
  every checksum before touching anything and keeps the previous states
  for rollback. Actions: `stage`, `install`, `drill`, `verify`,
  `failover`, `status`, `rollback`, `run`. Several targets are supported,
  each with its own root directory, compose project and hostname; the
  primary's hostname is substituted in the copies sent to a target that
  answers under a different name. A target with `CERT_COPY=true` gets the
  primary's certificate merged into the standby's ACME store with the
  ingress stopped, so it can serve the primary's hostname before DNS
  moves.
- `backup_offsite.py` — dated `tar.gz` of configured directories to
  another host over ssh (`CRON_BACKUP_OFFSITE`), verified on the far side
  (sha256 + `tar tzf`) and pruned to `OFFSITE_KEEP`. Live SQLite files
  listed in `OFFSITE_DB_PATHS` are snapshotted rather than tarred from
  disk, where they would land torn. `OFFSITE_PATHS` and
  `CLONE_TARGET_PATHS` / `CLONE_SHARED_PATHS` accept wildcards, so
  `/data/*` archives whatever is mounted under `/data` and files whose
  name carries a date (GeoIP databases) keep being shipped after they
  rotate; an entry matching nothing fails the run instead of quietly
  dropping content.
- `sqlite_snapshot.py`, `remote.py`, `checkmk.py` — shared helpers:
  consistent hot copies of live SQLite files plus a row-count sanity gate
  (`CLONE_MIN_ROWS`), ssh/rsync transport configured from an env prefix,
  and an optional Checkmk local check (`CHECKMK_SPOOL_DIR`) whose spool
  filename carries a max age, so a cron that stops running altogether
  goes stale by itself.
- `openssh-client` and `rsync` in the image, for the two ssh jobs.

### Changed
- The entrypoint builds its cron table from a list, logs one line per
  enabled job, and names the missing env vars when a schedule is set but
  its prerequisites are incomplete.

## [0.2.1] - 2026-05-21

### Added
- `STDOUT_EXCLUDE`, `MATTERMOST_EXCLUDE`, `EMAIL_EXCLUDE` — per-sink
  fnmatch deny-list, subtracted from the corresponding `_INCLUDE` set.
  Lets operators say "everything except X" without enumerating every
  other category. Empty (default) keeps the historic include-only
  behaviour. Example: `STDOUT_INCLUDE=*` + `STDOUT_EXCLUDE=peer.login.expired`
  to silence the most frequent noise on stdout without losing the rest.

### Changed
- Startup-probe wording simplified ("birdseye startup test" → "birdseye
  startup") in the email subject/body and the Mattermost message.

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
