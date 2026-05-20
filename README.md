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
ticker sometimes misses.

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
| `STDOUT_INCLUDE` | `*` | Per-sink fnmatch glob list |
| `MATTERMOST_INCLUDE` | `*` | |
| `EMAIL_INCLUDE` | `policy.*,user.*,setupkey.*,personalaccesstoken.*,account.*` | |
| `MATTERMOST_WEBHOOK_URL` | _(empty = disabled)_ | Mattermost incoming webhook |
| `MATTERMOST_USERNAME` | `NetBird` | Bot username on the webhook |
| `EMAIL_MODE` | `off` | `off` \| `immediate` \| `digest` |
| `EMAIL_DIGEST_MINUTES` | `15` | Digest flush interval |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_TO` / `SMTP_STARTTLS` | _(empty)_ | SMTP settings (`SMTP_TO` is comma-separated) |
| `CRON_CLEANUP_EPHEMERAL` | `*/15 * * * *` | Empty disables the cron job |
| `TZ` | `UTC` | Timezone for displayed timestamps |

## What's in the image

The image bundles the long-running forwarder plus the operator scripts
that were already in this repo. `supervisord` is PID 1, supervising:

- `event_forwarder.py` — long-running audit poller
- `cron -f` — runs `cleanup_ephemeral.py` on the `CRON_CLEANUP_EPHEMERAL`
  schedule

The one-shot operator scripts are also baked in and can be invoked via
`docker exec`:

```bash
docker exec birdseye uv run /app/list_policies.py
docker exec birdseye uv run /app/netbird_overview.py
docker exec birdseye uv run /app/cleanup_ephemeral.py --dry-run
docker exec birdseye uv run /app/allow_ping.py --help
docker exec birdseye uv run /app/manage_posture.py --help
docker exec birdseye uv run /app/setup_keys.py --help
```

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
