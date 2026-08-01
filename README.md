# birdseye

Bird's-eye view of a self-hosted [NetBird](https://netbird.io) deployment:
a long-running audit-event forwarder plus a handful of operator scripts,
packaged as a single Docker image you can run alongside your existing
NetBird `docker-compose` stack.

> Targets **self-hosted** NetBird (not NetBird Cloud). Uses the unofficial
> [`netbird`](https://pypi.org/project/netbird/) PyPI SDK
> (community-maintained, not affiliated with NetBird).

## What it does

The `birdseye` container polls `/api/events/audit` on your NetBird
management API and fans matching events out to three sinks:

| Sink         | Format                     | Toggle                              |
|--------------|----------------------------|-------------------------------------|
| **stdout**   | One line per event         | always on (read with `docker logs`) |
| **Mattermost** | Compact markdown via incoming webhook, one message per poll | `MATTERMOST_WEBHOOK_URL` empty → disabled |
| **Email**    | Plain text via SMTP        | `EMAIL_MODE=off \| immediate \| digest` |

It also runs `cleanup_ephemeral.py` on a cron schedule (default every
15 min) to delete stale ephemeral peers that NetBird's built-in cleanup
ticker sometimes misses, and an optional weekly
[backup](#weekly-backup) that mails two encrypted 7z archives: a
volume snapshot for byte-identical restore, and an API config export
in readable JSON.

Three further optional jobs copy the deployment somewhere else — a
configuration mirror onto a second controller, a database clone onto a
standby you can fail over to, and a dated config archive over ssh. See
[Replication and off-host copies](#replication-and-off-host-copies).

Highlights:

- **No event loss across restarts** — `last_id` persisted to a named
  volume, resumes exactly where it left off.
- **Bounded catch-up** — if the container's been down for a while,
  `MAX_CATCHUP` (default 200) caps how many backlog events get
  forwarded to Mattermost/email so a 3-day outage doesn't flood your
  channel.
- **Self-alert on extended API outage** — if the NetBird API is
  unreachable for more than `OUTAGE_ALERT_MINUTES` (default 10), the
  forwarder posts a `🚨 API unreachable` message to Mattermost (which
  usually lives on a different host) and a recovery message when
  polling resumes.
- **Per-sink filters** — each sink takes a comma-separated list of
  `fnmatch` globs over `activity_code`. Defaults: stdout/Mattermost see
  everything, email is curated to config-change events
  (`policy.*,user.*,setupkey.*,personalaccesstoken.*,account.*`).

## Quick start

Pre-built images are published per-release to Docker Hub and GHCR:

- `styliteag/birdseye:latest`
- `ghcr.io/styliteag/birdseye:latest`

Clone the repo for the compose file and env template, then:

```bash
cd docker/
cp .env.example .env
# Edit .env — minimum: NB_URL, NB_API_KEY, NB_ADMIN_API_KEY,
# MATTERMOST_WEBHOOK_URL (or leave empty), TZ.
docker compose up -d
docker compose logs -f
```

Once running you should see `[forwarder] first boot — seeded last_id=N,
no backlog forwarded`. Trigger any audit event in NetBird (e.g. toggle a
policy) to confirm the pipeline works.

## Running alongside your self-hosted NetBird

You can deploy birdseye in two ways. Pick one.

### Option A — separate stack, public hostname (simpler)

birdseye runs as its own `docker compose` project, talks to NetBird over
its public DNS name. Zero coupling between the two stacks.

In `docker/.env`:

```bash
NB_URL=https://netbird.example.com
NB_API_KEY=nbp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Then `docker compose up -d` from inside `docker/`. This is the default
the shipped `docker-compose.yml` uses — no edits needed.

### Option B — same docker network as NetBird (no public roundtrip)

Join the docker network that your NetBird services share. The
forwarder reaches the management API by internal hostname, so traffic
never leaves the host.

First, find your NetBird network name:

```bash
docker network ls | grep netbird
# Typical output: netbird_default
```

Then edit `docker/docker-compose.yml` — uncomment the `networks:`
blocks at the bottom and on the `birdseye` service, replacing the
network name to match what `docker network ls` showed:

```yaml
services:
  birdseye:
    # ... existing config ...
    networks:
      - netbird

networks:
  netbird:
    external: true
    name: netbird_default     # match `docker network ls`
```

And in `docker/.env`, point `NB_URL` at the internal service name
(check `docker compose ps` in the NetBird stack to see what your
management service is named — typically `management` or
`netbird-management`):

```bash
NB_URL=http://management:33073
```

The port (`33073` here) varies by NetBird version and how your
self-hosted compose exposes the management API. Check the NetBird
management container's ports with `docker port netbird-management`.

### Option C — merge into your NetBird compose file

If you'd rather have one `docker-compose.yml` for everything, copy the
`birdseye:` service block from `docker/docker-compose.yml` into your
existing NetBird compose file, plus the `birdseye-state` volume. The
service can then reference NetBird services directly without an
`external: true` network declaration.

## Configuration reference

All knobs are env vars. Full list with defaults in
[`docker/.env.example`](docker/.env.example). Most important:

| Env var | Default | Purpose |
|---|---|---|
| `NB_URL` | _(required)_ | NetBird management URL |
| `NB_API_KEY` | _(required)_ | Read-only API token (forwarder) |
| `NB_ADMIN_API_KEY` | _(optional)_ | Admin token for `cleanup_ephemeral` cron job |
| `POLL_INTERVAL` | `60` | Seconds between audit-API polls |
| `MAX_CATCHUP` | `200` | Cap on backlog events forwarded per restart |
| `OUTAGE_ALERT_MINUTES` | `10` | Mattermost self-alert threshold |
| `BACKLOG_WARN_THRESHOLD` | `1000` | Log a one-shot WARN when a single poll returns more than this many audit events. The NetBird audit endpoint has no cursor, so each poll re-downloads everything; growing past the threshold means it's time to lower server-side retention. |
| `STDOUT_INCLUDE` | `*` | Per-sink fnmatch glob list |
| `MATTERMOST_INCLUDE` | `*` | |
| `EMAIL_INCLUDE` | `policy.*,user.*,setupkey.*,personalaccesstoken.*,account.*` | |
| `STDOUT_EXCLUDE` | _(empty)_ | Subtracted from `STDOUT_INCLUDE`. Lets you say "everything except X" without listing every other category. Example: `STDOUT_INCLUDE=*` + `STDOUT_EXCLUDE=peer.login.expired` to drop the noisy expiry events from stdout. |
| `MATTERMOST_EXCLUDE` | _(empty)_ | Same semantics for the Mattermost sink. |
| `EMAIL_EXCLUDE` | _(empty)_ | Same semantics for the email sink. |
| `MATTERMOST_WEBHOOK_URL` | _(empty = disabled)_ | Mattermost incoming webhook |
| `MATTERMOST_USERNAME` | `birdseye` | Bot username on the webhook |
| `MATTERMOST_STARTUP_TEST` | `false` | When `true`, posts a one-shot smoke message to the webhook at container start so you can verify routing before the first audit event arrives. Failure is logged but does not abort the forwarder. |
| `EMAIL_STARTUP_TEST` | `false` | When `true`, sends a one-shot smoke mail (host, time, transport, recipients) at container start. Confirms SMTP host/port/TLS/credentials work without waiting for a configured event to fire. |
| `EMAIL_MODE` | `off` | `off` \| `immediate` \| `digest` |
| `EMAIL_DIGEST_MINUTES` | `15` | Digest flush interval |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_TO` | _(empty)_ | SMTP settings (`SMTP_TO` is comma-separated). When `SMTP_PORT` is empty the default is derived from `SMTP_TLS_MODE`: 587 / 465 / 25. |
| `SMTP_TLS_MODE` | _(legacy: `SMTP_STARTTLS=true` → `starttls`, else `none`)_ | Transport: `starttls` (submission, port 587), `tls` (implicit TLS / SMTPS, port 465), `none` (plain SMTP, port 25). |
| `SMTP_STARTTLS` | _(empty)_ | **Deprecated** — kept for backward compatibility. Prefer `SMTP_TLS_MODE`. `true` maps to `starttls`, `false` to `none`. |
| `CRON_CLEANUP_EPHEMERAL` | `*/15 * * * *` | Empty disables the cron job |
| `CRON_BACKUP_NETBIRD` | _(empty = disabled)_ | Cron schedule for `backup_volumes.py` (typical: `0 3 * * 0`) |
| `BACKUP_PATHS` | _(empty)_ | Comma-separated paths inside the container to back up |
| `BACKUP_ZIP_PASSWORD` | _(empty)_ | Passphrase for the AES256-encrypted 7z archive |
| `BACKUP_EMAIL_TO` | _(falls back to `SMTP_TO`)_ | Recipient(s) of the volume-backup mail |
| `EXPORT_EMAIL_TO` | _(falls back to `BACKUP_EMAIL_TO` → `SMTP_TO`)_ | Recipient(s) of the API-export mail |
| `BACKUP_MAX_ATTACHMENT_MB` | `20` | Above this, an error mail is sent in place of the attachment (applies to each mail) |
| `BACKUP_LABEL` | _(empty)_ | Free-form tag in the subject and filename (e.g. `prod`) |
| `BACKUP_EXCLUDE` | _(empty)_ | Comma-separated 7z wildcards excluded from the volume archive (case-insensitive, recursive) |
| `CRON_MIRROR_ACCOUNT` | _(empty = disabled)_ | Schedule for `mirror_account.py`. The scheduled run is a dry run unless `MIRROR_APPLY=true` |
| `CRON_CLONE_STANDBY` | _(empty = disabled)_ | Schedule for `clone_standby.py run` (typical: `17 */6 * * *`) |
| `CRON_BACKUP_OFFSITE` | _(empty = disabled)_ | Schedule for `backup_offsite.py` (typical: `42 3 * * *`) |
| `CRON_NETBIRD_MAINTENANCE` | _(empty = disabled)_ | Schedule for `netbird_maintenance.py` — attaches the posture check, then reconciles the ICMP companions (typical: `30 * * * *`) |
| `CHECKMK_SPOOL_DIR` | _(empty = disabled)_ | Mount your Checkmk agent's spool directory here and the unattended jobs write a local check. The filename carries a max age, so a cron that stops running goes stale on its own — something mail cannot tell you |
| `TZ` | `UTC` | Timezone for displayed timestamps |

The `MIRROR_*`, `CLONE_*` and `OFFSITE_*` settings behind the last three
are documented inline in [`docker/.env.example`](docker/.env.example) and
summarised in [Replication and off-host copies](#replication-and-off-host-copies).

## Weekly backup

A single cron entry (`CRON_BACKUP_NETBIRD`, e.g. `0 3 * * 0` for Sunday
03:00) drives two independent jobs and sends two independent mails:

1. **Volume snapshot** (`backup_volumes.py`) — packs mounted NetBird
   volumes into an encrypted 7z. For *byte-identical* restore.
2. **API config export** (`export_objects.py`) — fetches every
   configuration endpoint from the management API and stores it as
   readable JSON in a second encrypted 7z. For *seeing what was
   configured* on a given date.

Either step can be disabled by leaving its inputs empty: skip volumes
by leaving `BACKUP_PATHS` empty, skip the API export by leaving
`NB_ADMIN_API_KEY` empty. If both are set, both run sequentially in
the same job — a failure in one does not block the other.

Setup, step by step:

1. **Find the volumes you want to back up** in your NetBird stack:

   ```bash
   docker volume ls | grep netbird
   # Typical: netbird_management, netbird_signal, netbird_caddy_data
   ```

2. **Mount them read-only into birdseye.** Edit
   `docker/docker-compose.yml`, uncomment the example mounts under
   `volumes:` on the `birdseye` service, and the matching `external:
   true` declarations at the bottom. Match the volume names from step 1.

3. **Configure `.env`:**

   ```bash
   CRON_BACKUP_NETBIRD=0 3 * * 0          # Sunday 03:00
   # --- Volume snapshot ---
   BACKUP_PATHS=/backup/management,/backup/signal,/backup/caddy
   BACKUP_EXCLUDE=geo*                    # skip GeoIP DBs etc., optional
   BACKUP_EMAIL_TO=ops@example.com        # or leave empty to reuse SMTP_TO
   # --- API config export ---
   NB_ADMIN_API_KEY=nbp_xxxx              # admin scope to read all objects
   EXPORT_EMAIL_TO=                       # optional — falls back to BACKUP_EMAIL_TO, then SMTP_TO
   # --- Shared ---
   BACKUP_ZIP_PASSWORD=<long random passphrase, store offline>
   BACKUP_MAX_ATTACHMENT_MB=20
   BACKUP_LABEL=prod
   # SMTP_HOST / SMTP_PORT / SMTP_FROM / SMTP_USER / SMTP_PASSWORD
   # are reused from the existing email sink configuration.
   ```

4. **Trigger each one manually** to verify before relying on cron:

   ```bash
   docker compose exec birdseye \
     /app/.venv/bin/python /app/backup_volumes.py --dry-run
   docker compose exec birdseye \
     /app/.venv/bin/python /app/export_objects.py --dry-run
   # or both, in the same order the cron uses:
   docker compose exec birdseye /app/run_backup.sh
   ```

5. **Restore.** Both archives use the same `BACKUP_ZIP_PASSWORD`:

   ```bash
   # Volume restore — byte-identical:
   7z x netbird-prod-<timestamp>.7z
   # then stop NetBird, replace the volume contents, restart
   #
   # Config inspection — readable JSON:
   7z x netbird-export-<timestamp>.7z
   ls export/   # peers.json, groups.json, policies.json, … + manifest.json
   ```

If an archive exceeds `BACKUP_MAX_ATTACHMENT_MB`, you receive a
`— FAILED` mail with the actual size instead of a truncated attachment
— raise the limit, trim `BACKUP_PATHS`, or move to off-host storage.
The limit is checked against the **base64-encoded** payload (≈1.4× the
raw archive), which is the size SMTP servers actually count. Gmail
caps at 25 MB encoded, many corporate relays at 10 MB.

The cron line is only rendered when all of `CRON_BACKUP_NETBIRD`,
`BACKUP_ZIP_PASSWORD`, `SMTP_HOST`, `SMTP_FROM`, a recipient
(`BACKUP_EMAIL_TO` / `EXPORT_EMAIL_TO` / `SMTP_TO`), and at least one
source (`BACKUP_PATHS` or `NB_ADMIN_API_KEY`) are set. Missing
prerequisites print a one-line warning on startup and disable the
job.

### Caveat: live SQLite databases

NetBird's management service writes to `store.db` (SQLite) continuously.
A `7z` of the live file may capture an in-progress transaction and the
restored database can fail with `database is malformed` or silently lose
the last few writes. The Sunday-03:00 default minimises but does not
eliminate the risk.

For a strict hot-consistent backup of the management volume, either:

- **Pause NetBird briefly** before the backup (in a wrapper cron job)
  and resume afterwards:
  ```bash
  docker compose pause management && \
    docker compose exec birdseye /app/.venv/bin/python /app/backup_volumes.py; \
    docker compose unpause management
  ```
- **Or pre-snapshot the DB** with `sqlite3 ".backup"` and back up the
  snapshot file (works without stopping NetBird).

`clone_standby.py` and `backup_offsite.py` (below) do exactly that second
thing for you — see [`sqlite_snapshot.py`](sqlite_snapshot.py).

## Keeping the account's conventions in step

`manage_posture.py` and `allow_ping.py` are reconcilers: they compare the
account against a convention and fix the difference. Run by hand they only take
effect when someone remembers, so the account drifts in between — a policy
created on Tuesday has no posture check and no `ZPING:` companion until the next
manual pass. `netbird_maintenance.py` is what a cron entry calls so the
convention holds by itself:

```bash
CRON_NETBIRD_MAINTENANCE=30 * * * *
MAINTENANCE_POSTURE_CHECK=Posture-Europe   # an existing check; empty skips the step
MAINTENANCE_ALLOW_PING=true                # empty skips the step
```

Order is not configurable, and that is the point: **posture runs first**.
`allow_ping.py` copies each policy's `source_posture_checks` into its companion,
so attaching a check afterwards would leave the companions one cycle behind.

Both steps are idempotent — a run with nothing to do writes nothing, so an
hourly schedule produces no audit-event noise in your Mattermost channel. The
steps are independent: one failing does not stop the other, and every failure is
reported at the end.

To exempt individual policies from an automated run, use the markers the two
scripts already honour: `POSTURE_IGNORE` or `PING_IGNORE` in the policy
description. To watch before committing, set `MAINTENANCE_DRY_RUN=true` (or run
it by hand with `--dry-run`); each step's own summary line
(`Summary: created=… updated=… deleted=…`) ends up in the Checkmk check, so a
reconciler that suddenly starts changing things every run is visible instead of
buried in a container log.

```bash
docker exec birdseye /app/.venv/bin/python /app/netbird_maintenance.py --dry-run
```

## Replication and off-host copies

Three optional jobs copy a NetBird deployment somewhere else. They solve
different problems and can be run together:

| Job | Copies | Good for | Not good for |
|---|---|---|---|
| `mirror_account.py` | configuration, via the API | keeping a second controller's config in step — lab, staging, second region | failover: peers cannot be created through the API |
| `clone_standby.py` | the database + config files, over ssh | **failover** — move one DNS record and the standby *is* the controller | reading an old value: it only holds the latest state |
| `backup_offsite.py` | whole directories, as dated `tar.gz` | digging an old compose file, `.env` or ACME store out of three weeks ago | fast recovery: it is an archive, not a running system |

Nothing about any particular deployment is baked in: every host, path,
directory and stack name is an env var, and each job disables itself when
its inputs are empty. Full list with comments in
[`docker/.env.example`](docker/.env.example).

### Account mirror

Copies posture checks, groups, networks, resources, routers, policies,
routes, setup keys, DNS, users and account settings from `NB_URL` onto a
second controller. Objects are matched **by name**, not by ID (IDs are
per-instance), so the sync is idempotent and can be re-run.

```bash
MIRROR_URL=https://netbird2.example.com
MIRROR_API_KEY=nbp_…
MIRROR_APPLY=true          # without this a scheduled run only reports drift
CRON_MIRROR_ACCOUNT=25 * * * *
```

The source is opened through a client that rejects every method except
`GET`, and the run aborts if both URLs resolve to the same host. Pruning
is on by default — this is a mirror, not an additive import; set
`MIRROR_PRUNE=false` if you want it to only ever add. Run it by hand
first, it is dry-run by default:

```bash
docker exec birdseye /app/.venv/bin/python /app/mirror_account.py
docker exec birdseye /app/.venv/bin/python /app/mirror_account.py --apply
```

Peers cannot be created through the API — they enrol themselves with a
setup key — so anything pointing at a peer is skipped until a peer of
that name exists on the target. That is also why this is not a failover
target.

### Standby clone

Copies the *database* to a host you can fail over to by moving one DNS
record. The live SQLite stores are read with SQLite's online backup API —
transactionally consistent, no downtime, source opened read-only —
checksummed into a payload with the config files, rsynced, and installed
by a generated `install.sh` that verifies every checksum before touching
anything and keeps the previous states for rollback.

```bash
CLONE_SSH_HOST=root@standby.example.com
CLONE_DB_PATHS=/data/netbird/store.db,/data/netbird/idp.db
CLONE_CONFIG_FILES=/data/stack/config.yaml,/data/stack/dashboard.env
CLONE_TARGETS=real,test
CLONE_REAL_ROOT=/root/nb-real       # the clone: the primary's own identity
CLONE_REAL_HOST=netbird.example.com #   …and hostname
CLONE_REAL_AUTOSTART=false          #   normally stopped
CLONE_REAL_CERT_COPY=true
CLONE_TEST_ROOT=/root/nb-test       # the smoke test: same data, own name
CLONE_TEST_HOST=standby.example.com
CLONE_TEST_AUTOSTART=true
CRON_CLONE_STANDBY=17 */6 * * *
```

Two targets is the useful arrangement. The **clone** carries the primary's
identity and stays stopped; the **smoke test** runs continuously under the
standby's own hostname and proves the data survived the trip without
pretending to be the primary. One payload serves both: in the copies sent
to a target whose `HOST` differs, the primary's hostname is substituted
inside the config files.

```bash
docker exec birdseye /app/.venv/bin/python /app/clone_standby.py stage
docker exec birdseye /app/.venv/bin/python /app/clone_standby.py install
docker exec birdseye /app/.venv/bin/python /app/clone_standby.py drill    # start, verify, stop
docker exec birdseye /app/.venv/bin/python /app/clone_standby.py status
```

`drill` is the one to schedule an eye on: it starts the clone, compares
account id and object counts against the live primary over a connection
that resolves the primary's hostname to the standby's address — no DNS
change, nothing else disturbed — and stops it again. `run` does
stage → install → drill in one go and is what `CRON_CLONE_STANDBY` calls.

Two things worth knowing before relying on it:

- **Certificates.** A standby cannot *issue* the primary's certificate
  while DNS still points at the primary — the CA would validate against
  the primary. A target with `CERT_COPY=true` therefore serves a copy,
  merged into the standby's ACME store on every run with the ingress
  stopped (Traefik reads that store only at startup and rewrites it from
  memory afterwards, so merging into a running instance is silently
  discarded). After a real failover the standby renews by itself over
  HTTP-01/TLS-ALPN, no DNS-provider account needed.
- **Peers enrolled since the last run are missing**, and dashboard
  sessions do not survive: the tokens were issued by the other instance.

`failover.sh` is written to the clone's directory on the standby, so a
failover works even when the primary — and this container with it — is
gone.

A failed refresh mails `CLONE_EMAIL_TO` (falling back to `BACKUP_EMAIL_TO`,
then `SMTP_TO`) and marks the `NetBird_Standby_Clone` check CRIT. Both matter:
mail tells you a run broke, the check's max age tells you a run stopped
happening at all.

### Offsite config archive

A dated `tar.gz` of whole directories on another host, verified after
transfer (sha256 + `tar tzf` on the far side) and pruned to `OFFSITE_KEEP`.

```bash
OFFSITE_SSH_HOST=root@standby.example.com
OFFSITE_REMOTE_DIR=/root/backups
OFFSITE_PATHS=/data/*                     # whatever is mounted under /data
OFFSITE_DB_PATHS=/data/netbird/store.db   # snapshotted, not tarred from disk
OFFSITE_EXCLUDE=*.mmdb,*.BIN              # large, regenerable
OFFSITE_KEEP=14
CRON_BACKUP_OFFSITE=42 3 * * *
```

Paths are stored relative to their common parent unless
`OFFSITE_BASE_DIR` says otherwise, so mounts at `/data/stack` and
`/data/traefik` yield an archive holding `stack/` and `traefik/`. A
wildcard entry is expanded at run time: `/data/*` archives whatever is
mounted under `/data`, so adding a mount needs no config change, and an
entry that matches nothing fails the run rather than quietly shrinking
the archive. The archives are written
`0600` and **contain secrets** — ACME private keys, `.env` files, store
encryption keys. Send them only somewhere that already holds data of the
same sensitivity.

### SSH access

Both ssh jobs need a key that can reach the far side; the clone also needs
docker there. Mount the key read-only and point the job at it:

```yaml
volumes:
  - ./standby_key:/run/secrets/standby_key:ro
  - /opt/stacks/netbird/data/netbird_data:/data/netbird:ro
  - /opt/stacks/netbird:/data/stack:ro
```

```bash
CLONE_SSH_KEY=/run/secrets/standby_key
CLONE_SSH_STRICT=accept-new      # trust on first use (default)
```

`accept-new` accepts an unknown host key once and pins it. To verify the
first connection too, mount a `known_hosts` file, point
`CLONE_SSH_KNOWN_HOSTS` at it and set `CLONE_SSH_STRICT=yes`.

Everything these two jobs write happens on the far side: locally they only
read, and the docker socket (needed only for `CLONE_IMAGES`, which pins the
standby to the image digests the primary runs) can be mounted read-only.

## What's in the image

The image bundles the long-running forwarder plus the operator scripts
that were already in this repo. `supervisord` is PID 1, supervising:

- `event_forwarder.py` — long-running audit poller
- `cron -f` — runs `cleanup_ephemeral.py` on the `CRON_CLEANUP_EPHEMERAL`
  schedule and, when configured, `run_backup.sh` on `CRON_BACKUP_NETBIRD`
  (which sequentially invokes `backup_volumes.py` and `export_objects.py`
  depending on what is configured), plus `netbird_maintenance.py`,
  `mirror_account.py`, `clone_standby.py run` and `backup_offsite.py` on their
  own schedules

A job whose prerequisites are incomplete is not installed at all; the
entrypoint logs which env vars are missing and lists the schedules it did
enable, so `docker logs birdseye | head` tells you what is actually armed.

The one-shot operator scripts are also baked in and can be invoked via
`docker exec`:

```bash
docker exec birdseye /app/.venv/bin/python /app/list_policies.py
docker exec birdseye /app/.venv/bin/python /app/netbird_overview.py
docker exec birdseye /app/.venv/bin/python /app/cleanup_ephemeral.py --dry-run
docker exec birdseye /app/.venv/bin/python /app/allow_ping.py --help
docker exec birdseye /app/.venv/bin/python /app/manage_posture.py --help
docker exec birdseye /app/.venv/bin/python /app/setup_keys.py --help
docker exec birdseye /app/.venv/bin/python /app/mirror_account.py        # dry run
docker exec birdseye /app/.venv/bin/python /app/clone_standby.py status
docker exec birdseye /app/.venv/bin/python /app/backup_offsite.py --list
```

> `uv` lives only in the builder stage, so inside the running image use
> the venv interpreter directly. The same pattern applies to the
> backup scripts shown earlier in this README.

## Local development (without Docker)

If you'd rather hack on the scripts directly:

```bash
uv sync
cp .env.example .env   # at repo root, edit with NB_URL + NB_API_KEY
uv run events.py                          # streaming console viewer (the dev predecessor of event_forwarder)
uv run list_policies.py                   # one-shot
uv run docker/event_forwarder.py          # forwarder, with /var/lib/birdseye replaced by $STATE_FILE
```

## Releases

[`./release.sh`](release.sh) bumps the version, updates `CHANGELOG.md`,
tags the commit, and pushes — which triggers the
[release-docker workflow](.github/workflows/release-docker.yml) to build
and publish multi-arch images to Docker Hub and GHCR.

```bash
./release.sh patch    # 0.1.0 → 0.1.1 (default)
./release.sh minor    # 0.1.0 → 0.2.0
./release.sh major    # 0.1.0 → 1.0.0
```

## Notes

- The `netbird` PyPI package is community-maintained and **not**
  affiliated with NetBird. Some of its pydantic models reject valid
  values (notably the `netbird-ssh` protocol enum) — `allow_ping.py`
  and `manage_posture.py` work around this by bypassing the typed write
  path and calling `client.post()` / `client.put()` with raw dicts. See
  [`CLAUDE.md`](CLAUDE.md) for the gotcha details.
- Network-traffic events (`/api/events/network-traffic`) are
  **cloud-only**; the audit-event endpoint is the only event stream
  available on self-hosted NetBird. Tracking upstream issue:
  [netbirdio/netbird#3935](https://github.com/netbirdio/netbird/issues/3935).

## License

[MIT](LICENSE)
