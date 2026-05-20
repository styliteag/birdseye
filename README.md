# birdseye

Small Python scripts for working with the [NetBird Public API](https://docs.netbird.io/api),
using the unofficial [`netbird`](https://pypi.org/project/netbird/) PyPI client.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Python 3.12 (uv will install it if missing)
- A NetBird API token — create one in the NetBird dashboard under
  **Settings → Users → (your user) → Create Access Token**

## Setup

Clone the repo and install dependencies:

```bash
uv sync
```

Create a `.env` file in the project root:

```bash
NB_URL=https://api.netbird.io
NB_API_KEY=nbp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

For self-hosted NetBird, set `NB_URL` to your management URL
(e.g. `https://netbird.example.com`).

> `.env` is gitignored — never commit your API key.

## Scripts

### `list_policies.py`

Lists all access-control policies in your NetBird tenant.

```bash
uv run list_policies.py
```

Output:

```
d85i36n0cv2c73e94bjg  'Allow Ping for all'      enabled=True  rules=1
d85i3of0cv2c73e94c80  'Mon Server to Clients'   enabled=True  rules=1
...
```

Columns: policy ID, name, enabled flag, number of rules.

## Project layout

```
.
├── .env              # secrets (gitignored)
├── list_policies.py  # list all policies
├── pyproject.toml    # uv project + dependencies
└── uv.lock           # locked dependency versions
```

## Notes

- The `netbird` package is community-maintained and **not** affiliated with
  NetBird. For production use, consider calling the REST API directly with
  `httpx` against the [official API spec](https://docs.netbird.io/api).
- The client wants a bare host (no scheme); `list_policies.py` strips
  `https://` from `NB_URL` automatically.
