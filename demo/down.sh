#!/bin/bash
# Stop the demo and delete all its data (account, peers, snapshots, secrets).
set -euo pipefail
cd "$(dirname "$0")"
docker compose -f compose.yml $( [ -f peers.yml ] && echo "-f peers.yml" ) down --remove-orphans
rm -rf data peers.yml secrets.env web.env config.yaml
echo "demo removed"
