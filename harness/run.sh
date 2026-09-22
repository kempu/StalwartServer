#!/usr/bin/env bash
# Run the search harness. See README.md ("The search harness").
#
#   harness/run.sh stock   [IMAGE]                   seed, then expect stock answers
#   harness/run.sh patched IMAGE                     seed, then expect patched answers
#   harness/run.sh rollout PATCHED_IMAGE [STOCK]     the production rollout on one database:
#                                                    stock -> patched over stale rows -> reindex
#
# IMAGE defaults to UPSTREAM_IMAGE, upstream's own image of the release upstream.lock pins. Every run starts
# from an empty database and removes it again; the passwords are generated per run and
# never leave this machine.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
. ../upstream.lock

export HARNESS_PG_PASSWORD HARNESS_ADMIN_PASSWORD HARNESS_USER_PASSWORD STALWART_IMAGE
HARNESS_PG_PASSWORD="$(openssl rand -hex 16)"
HARNESS_ADMIN_PASSWORD="$(openssl rand -hex 16)"
HARNESS_USER_PASSWORD="$(openssl rand -hex 16)"

down() { docker compose down -v --remove-orphans >/dev/null 2>&1 || true; }
up() {
  STALWART_IMAGE="$1"
  echo "== server: $STALWART_IMAGE"
  docker compose up -d --wait --quiet-pull >/dev/null
}
trap down EXIT
down

case "${1:-}" in
  stock)
    up "${2:-$UPSTREAM_IMAGE}"
    python3 probe.py seed
    python3 probe.py check --expect stock
    ;;
  patched)
    up "${2:?give the image to test}"
    python3 probe.py seed
    python3 probe.py check --expect patched
    ;;
  rollout)
    patched="${2:?give the patched image}"
    up "${3:-$UPSTREAM_IMAGE}"
    python3 probe.py seed
    python3 probe.py check --expect stock
    # Same database, new server: every row was indexed by stock.
    up "$patched"
    python3 probe.py check --expect patched-stale
    python3 probe.py reindex
    python3 probe.py check --expect patched
    ;;
  *)
    sed -n '2,12p' "$0" >&2
    exit 2
    ;;
esac
