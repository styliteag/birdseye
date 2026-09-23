# birdseye-web — setup guide

`birdseye-web` is a small web app that sits next to your self-hosted NetBird
and answers one question well: **who can reach what?** It also lets you change
the answer — groups, group membership and policies — with a preview of the
effect before anything is written.

It ships as its own image (`styliteag/birdseye-web`), separate from the
`birdseye` forwarder/cron image. You can run either without the other.

## What you get

| Page | What it does |
|---|---|
| **Matrix** | Who may access whom, as a grid. Five views: Group × Group, Peer × Peer, Group × Resource, Peer × Resource, User × Destination. Filter by name, protocol, port; hide “All”; show only posture-gated access. Click a cell to see *which* policy allows it. |
| **Matrix editing** | In the group views: click a filled cell to remove a group from the rule, disable or delete the policy; click an empty cell to create an allow policy. Every action shows the gained/lost access first and only writes on *Confirm*. |
| **Reachability** | “Can peer X reach Y on tcp/22?” with the policies and groups that make it so. |
| **Groups** | Create, rename, delete; edit membership with a searchable checklist. A live panel shows who gains or loses access *before* you save. |
| **Policies** | List with on/off switch; editor for multi-rule policies (groups or a single resource/peer as destination, `netbird-ssh`, ports and ranges, bidirectional, posture checks) with the same live effect preview. |
| **Anomalies** | Configuration smells: rules that bypass groups, devices whose groups drifted from their user's defaults, peers inside resource groups, empty or missing groups in policies, resources nobody routes or reaches, unused groups, disabled policies. |

Writes go to NetBird **immediately** (after the confirm/preview step) and show
up in NetBird's audit log under the signed-in user's name.

## How sign-in works (and why there is no API key)

birdseye-web has no users and no API key of its own. You sign in with your
**NetBird account** through NetBird's embedded identity provider, and every API
call is made with *your* token:

- NetBird enforces your role — a user who may not edit policies cannot edit
  them here either (the UI also hides what you may not do).
- The NetBird audit log shows the real person, not a service key.

Technically it is an OIDC authorization-code flow with PKCE against
`<NB_URL>/oauth2`, using NetBird's own public client `netbird-dashboard`. The
management API only accepts tokens issued to `netbird-dashboard` or
`netbird-cli`, which is why a separate client would not work. Tokens are kept
in server memory only; the browser gets a random session ID in an `HttpOnly`,
`__Host-` cookie. A container restart signs everyone out.

> This relies on the embedded IdP of NetBird (local users; issuer
> `https://<your-netbird>/oauth2`). It is how the dashboard itself signs in,
> but not a documented integration point — verify after NetBird upgrades.

## 1. Register the callback URL in NetBird

The IdP only redirects to URLs it knows, matched **exactly** (scheme, host,
path, no trailing slash). Add birdseye-web's callback to the dashboard client:

**Combined setup** (`config.yaml`, created by `getting-started.sh`):

```yaml
server:
  auth:
    issuer: "https://netbird.example.com/oauth2"
    dashboardRedirectURIs:
      - "https://netbird.example.com/nb-auth"          # keep
      - "https://netbird.example.com/nb-silent-auth"   # keep
      - "https://birdseye.example.com/auth/callback"   # add: <WEB_BASE_URL><WEB_REDIRECT_PATH>
```

**Older setup** (`management.json`): add the same URL to
`EmbeddedIdP.DashboardRedirectURIs`.

Then restart the NetBird server (`docker compose restart` in the NetBird
stack). Peers reconnect for a few seconds.

Do **not** remove the existing entries — the NetBird dashboard needs them.
NetBird applies this list to both of its clients (`netbird-dashboard` and
`netbird-cli`); that is harmless as long as the URL is your own host.

## 2. Run the container

```bash
cd docker/web
cp .env.example .env      # then edit, see below
docker compose up -d
docker compose logs -f
```

`docker/web/docker-compose.yml` pulls `styliteag/birdseye-web:latest`. The
container is read-only, runs as an unprivileged user and publishes port 8080
on `127.0.0.1` only — put your reverse proxy (TLS) in front of it.

### Environment

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WEB_NB_URL` | yes (or `NB_URL`) | – | NetBird URL, e.g. `https://netbird.example.com` |
| `WEB_BASE_URL` | yes | – | Public URL of birdseye-web as the browser sees it, e.g. `https://birdseye.example.com`. With `https` the session cookie is `Secure` + `__Host-` and HSTS is sent. |
| `WEB_SESSION_SECRET` | yes | – | ≥ 32 random characters (`openssl rand -hex 32`). |
| `WEB_REDIRECT_PATH` | no | `/auth/callback` | Callback path. Must match what you registered in step 1. |
| `WEB_OIDC_CLIENT_ID` | no | `netbird-dashboard` | Only change if you know why. |
| `WEB_OIDC_ISSUER` | no | `<WEB_NB_URL>/oauth2` | Embedded IdP issuer. |
| `WEB_CACHE_TTL` | no | `30` | Seconds a per-user snapshot of the account is reused. Every write clears it. |
| `WEB_SESSION_HOURS` | no | `12` | Session lifetime. |
| `WEB_ANOMALY_IGNORE` | no | – | Regex on group names the Anomalies page never reports, e.g. `^Z[0-9]{3}\b` for groups used as documentation notes. |
| `TZ` | no | `UTC` | Log timestamps. |

The container refuses to start and names the missing variable when the
configuration is incomplete.

### Reverse proxy

Any proxy works; it only needs to forward HTTPS to `127.0.0.1:8080`. Example
for Caddy:

```
birdseye.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Traefik labels, if birdseye-web joins the proxy's network instead of
publishing a port:

```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.birdseye.rule=Host(`birdseye.example.com`)
      - traefik.http.routers.birdseye.entrypoints=websecure
      - traefik.http.routers.birdseye.tls.certresolver=letsencrypt
      - traefik.http.services.birdseye.loadbalancer.server.port=8080
```

Keep **one** replica / worker: sessions live in process memory.

## 3. Try it locally first (no NetBird change needed)

NetBird already allows `http://localhost:53000/` as a redirect (it is the
`netbird up` CLI callback). So on your workstation:

```bash
uv sync
WEB_NB_URL=https://netbird.example.com \
WEB_BASE_URL=http://localhost:53000 \
WEB_REDIRECT_PATH=/ \
WEB_SESSION_SECRET=$(openssl rand -hex 32) \
uv run uvicorn --factory birdseye_web.app:build --host 127.0.0.1 --port 53000 --reload --reload-dir birdseye_web
```

Open <http://localhost:53000>. Port 53000 must be free (a running `netbird up`
login uses it briefly).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| NetBird shows *“Unregistered redirect_uri”* | The callback is not in `dashboardRedirectURIs`, or differs by scheme, host, path or a trailing slash. Restart NetBird after editing. |
| Login page hangs, `https://<netbird>/oauth2/keys` does not answer | NetBird's embedded IdP is stuck; restart the NetBird server. (`/oauth2/healthz` returns 502 on current NetBird regardless — a known nil-pointer in the embedded IdP, not a sign of trouble.) |
| *“Login session expired or invalid”* | The login took longer than 10 minutes, or the container restarted in between. Sign in again. |
| Signed out after an update | Expected: sessions are in memory. |
| Buttons for editing are missing | Your NetBird role lacks `policies`/`groups` update permission. |
| A change made in the NetBird dashboard is not visible | The snapshot is cached for `WEB_CACHE_TTL` seconds; reload after that. |

## Security notes

- CSRF token on every write, strict Content-Security-Policy (no inline
  script), `X-Frame-Options: DENY`, HSTS on https.
- Object IDs are validated before they reach an API path.
- Pending (not yet signed-in) sessions expire after 10 minutes and are capped.
- Every write is logged as one `birdseye_web.audit` line: who, what, before → after.
