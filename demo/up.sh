#!/bin/bash
# Start the demo: NetBird (server + dashboard), birdseye, birdseye-web, 10 peers.
# First run sets up and seeds the account; later runs just start everything.
set -euo pipefail
cd "$(dirname "$0")"
UV="uv run --project .."

if [ ! -f config.yaml ]; then
  r() { openssl rand -base64 32; }
  sed -e "s|@@RELAY_SECRET@@|$(r | tr -d '=')|" -e "s|@@COOKIE_KEY@@|$(r)|" \
      -e "s|@@STORE_KEY@@|$(r)|" config.yaml.tpl > config.yaml
fi
[ -s web.env ] || echo "WEB_SESSION_SECRET=$(openssl rand -hex 32)" > web.env
touch secrets.env
mkdir -p data/birdseye
export DEMO_VERSION="$(cat ../VERSION)" DEMO_REVISION="$(git -C .. rev-parse HEAD)"

first_run=false
docker compose up -d traefik netbird-server dashboard
if [ ! -f peers.yml ]; then
  first_run=true
  $UV python seed.py                       # owner + token, groups, policies, networks, keys, users
fi
docker compose -f compose.yml -f peers.yml up -d --build
echo "waiting for peers to enroll…"
sleep 20
$UV --with playwright python login_users.py   # user-owned laptops: device login as each user

if $first_run; then
  $UV python seed.py late                  # drop NetBird defaults, direct-peer policy, drift
  echo "config history: baseline + two rounds of changes…"
  rm -rf data/birdseye/jobs/history/*
  docker exec demo-birdseye /app/.venv/bin/python /app/jobrun.py run history --trigger manual -- \
    /app/.venv/bin/python /app/config_history.py --force >/dev/null
  $UV python changes.py
fi

set -a; . ./secrets.env; set +a
cat <<MSG

  birdseye-web:      http://localhost:58090
  NetBird dashboard: http://netbird.localhost:58080
  login:             $DEMO_OWNER_EMAIL / $DEMO_OWNER_PASSWORD
  screenshots:       $UV --with playwright python shoot.py
MSG
