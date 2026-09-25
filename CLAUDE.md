# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Small Python toolkit for the NetBird Public API. Targets a **self-hosted** NetBird instance (not NetBird Cloud). Uses the unofficial [`netbird`](https://pypi.org/project/netbird/) PyPI SDK (community-maintained, not affiliated with NetBird).

## Running

- Python 3.12, managed by [`uv`](https://docs.astral.sh/uv/). Install deps with `uv sync`.
- Invoke scripts as `uv run <script>.py` — **no `python` between `run` and the filename**.
- Required env vars in `.env` (gitignored, loaded via `python-dotenv`):
  - `NB_URL` — e.g. `https://netbird.example.com`
  - `NB_API_KEY` — bearer token (`nbp_…`)

## Critical write-path gotcha

The SDK's pydantic `Protocol` enum rejects valid NetBird protocol values (notably `netbird-ssh`). **For write operations (POST/PUT to `/policies` and similar), bypass `PolicyCreate` / `PolicyUpdate` / `PolicyRule` and call the underlying HTTP layer with raw dicts:**

```python
client.post("policies", data=payload)   # not client.policies.create(PolicyCreate(...))
client.put(f"policies/{pid}", data=payload)
```

Reads (`client.policies.list()`, `.get()`) are fine — only the model-based write path is broken. Working references: `manage_posture.py`, `allow_ping.py`.

When constructing raw payloads, flatten embedded group objects (returned by `GET`) to lists of group IDs (`[g["id"] for g in rule["sources"]]`) — the API expects IDs on write.

## Self-hosted limits

- `/api/events/network-traffic` (blocked-packet / traffic events) is **cloud-only** and returns plain-text `404 page not found` on this self-hosted instance. Only `/api/events/audit` is available. Tracking: [netbirdio/netbird#3935](https://github.com/netbirdio/netbird/issues/3935).
- Event-streaming integrations (Generic HTTP, Datadog, S3, Firehose) are likewise cloud-only.

## Conventions for new write scripts

- Build the client with `nb_client.client_from_env(key="user"|"admin")` (reads `NB_URL` plus `NB_API_KEY` / `NB_ADMIN_API_KEY`, returns `APIClient`).
- Always support `--dry-run`.
- Print a before/after line for every mutation so the audit trail is visible in stdout.

## Jobs that copy a deployment somewhere else

`mirror_account.py` (API → second controller), `clone_standby.py` (database → failover host) and `backup_offsite.py` (directories → dated archives). Rules that keep them installation-agnostic:

- **No hostnames, paths or stack names in the code.** Everything is an env var with a per-job prefix (`MIRROR_`, `CLONE_`, `OFFSITE_`), read through `backup_common.env*`. A job disables itself when its inputs are empty, and says which ones are missing.
- SSH goes through `remote.Remote.from_env("<PREFIX>")` — do not shell out to `ssh` directly. Remote work is a bash script fed to that object on stdin, so it stays one round trip and one place to read.
- Live SQLite files are copied with `sqlite_snapshot.snapshot()`, never `cp`/`tar`.
- Unattended jobs must alert on **configuration** errors too (an unmounted path, a renamed target), not only on a step that fails — otherwise the failure only exists in the container log. Mail via `backup_common`, plus `checkmk.write()` when `CHECKMK_SPOOL_DIR` is set.
- Anything generated and shipped (`install.sh`, `failover.sh`) is rendered from a template with `@@TOKEN@@` substitution and must stay readable: someone will run it by hand on the far side during an outage.

## Web UI (`birdseye_web/`)

FastAPI + Jinja + HTMX, own image (`docker/web/Dockerfile`). User docs: `docs/web-ui.md`.

- **No API key.** Users sign in through NetBird's embedded IdP as the public client `netbird-dashboard` (auth code + PKCE); the management API accepts no other audience. Every call goes through `nbapi.NetBirdAPI` with `Authorization: Bearer <user token>` — the SDK hardcodes `Token`, so it is not used here. Never add a service key: NetBird's role checks and audit log depend on the user's own token.
- **Layers:** `models.py` (frozen dataclasses parsed from raw API dicts) → `access.py` (`grants()` per rule, `edges()` expanded to peers/resources) → `matrix.py` / `diff.py` / `anomalies.py`. All views read the same grants, so they cannot disagree. Keep these pure and tested; routes only do HTTP.
- **Writes:** raw dicts via `payloads.py` / `quickedit.py`. Group `PUT` replaces the whole group — always re-`GET` first and carry `resources` over. Policy updates start from a fresh `GET` and keep fields the editor does not show (`authorized_groups`, `sourceResource`).
- Every write: CSRF (`context.csrf_protect`), `context.log_change()` before/after line, `cache.invalidate()`. Previews use `diff.access_delta()` on a simulated snapshot.
- UI text is English. No inline JS/`hx-on` (strict CSP); behaviour lives in `static/app.js`.
- **Jobs page:** the web container never runs anything itself. It reads `registry.json`/`state/*.json` from the shared jobs dir and drops request files; `jobrun.py serve` in the birdseye container validates them against its registry and `JOB_TRIGGERS`. Keep that split — no docker socket, no command lines from the web side. New cron jobs go through `add_job <key> …` / `job_off` in `docker/entrypoint.sh`.
- Run locally: see `docs/web-ui.md` step 3 (`http://localhost:53000/` is a preregistered redirect). Tests: `uv run pytest`.

## Commit style

Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`, `perf:`, `ci:`. No `Co-Authored-By` trailers.
