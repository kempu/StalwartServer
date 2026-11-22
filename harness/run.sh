#!/usr/bin/env bash
# Run the search harness. See README.md ("The search harness").
#
#   harness/run.sh stock   [IMAGE]                   seed, then expect stock answers
#   harness/run.sh patched IMAGE                     seed, then expect patched answers
#   harness/run.sh rollout PATCHED_IMAGE [STOCK]     the production rollout on one database:
#                                                    stock -> patched over stale rows -> reindex
#   harness/run.sh push    [IMAGE]                   which deliveries a push subscription's
#                                                    emailPush filter lets through (push/push_probe.py)
#   harness/run.sh sharing EXPECT [IMAGE]            whether a person a collection is shared with chooses
#                                                    its subscription (sharing/sharing_probe.py); EXPECT
#                                                    is stock or patched
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

COMPOSE=(docker compose -f docker-compose.yml)
if [ "${1:-}" = push ]; then
  # The push catcher's certificate, from a CA made for this run alone. The server under test
  # trusts that CA through SSL_CERT_FILE; nothing else does, and the directory goes on exit.
  export HARNESS_PUSH_CERTS
  HARNESS_PUSH_CERTS="$(mktemp -d)"
  (
    cd "$HARNESS_PUSH_CERTS"
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=push harness CA" \
      -keyout ca.key -out ca.pem 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj "/CN=push.harness.test" \
      -keyout push.key -out push.csr 2>/dev/null
    printf 'subjectAltName=DNS:push.harness.test\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > ext
    openssl x509 -req -in push.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 1 \
      -extfile ext -out push.pem 2>/dev/null
    cp ca.pem bundle.pem
    rm ca.key push.csr ext
    chmod 644 ./*
  )
  COMPOSE+=(-f push/docker-compose.push.yml)
fi

down() {
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  if [ -n "${HARNESS_PUSH_CERTS:-}" ]; then rm -rf "$HARNESS_PUSH_CERTS"; fi
}
up() {
  STALWART_IMAGE="$1"
  echo "== server: $STALWART_IMAGE"
  "${COMPOSE[@]}" up -d --wait --quiet-pull >/dev/null
}
trap down EXIT
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

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
    python3 probe.py check --expect reindexed
    ;;
  push)
    up "${2:-$UPSTREAM_IMAGE}"
    python3 push/push_probe.py
    ;;
  sharing)
    expect="${2:?give stock or patched}"
    up "${3:-$UPSTREAM_IMAGE}"
    python3 sharing/sharing_probe.py "$expect"
    ;;
  *)
    sed -n '2,17p' "$0" >&2
    exit 2
    ;;
esac
