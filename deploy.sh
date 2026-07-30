#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo 'Run as root: sudo ./deploy.sh' >&2
  exit 1
fi

cd /opt/open-wearables

# Bootstrap mode: op-refs.env holds literal values (vps-op-run SA is read-only).
# To migrate to 1P: create Infra item open-wearables, replace literal values in
# op-refs.env with op:// references, then swap this block to:
#   op run --env-file=op-refs.env -- docker compose -f docker-compose.vps.yml up -d --build

echo '[ow] loading secrets...'
set -a; source /opt/open-wearables/op-refs.env; set +a

echo '[ow] building + starting stack...'
docker compose -f docker-compose.vps.yml up -d --build

echo '[ow] waiting for backend (up to 200s)...'
for i in $(seq 1 40); do
  if docker exec ow-backend curl -sf http://localhost:8000/ > /dev/null 2>&1; then
    echo '[ow] backend healthy'
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done

echo '[ow] stack status:'
docker compose -f docker-compose.vps.yml ps
