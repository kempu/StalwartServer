#!/usr/bin/env bash
# Build the image exactly as the published one is built: the publish gate and the pinned upstream
# commit with patches/ applied (scripts/ci-prepare.sh), then one `docker buildx build` for
# linux/amd64 whose every build argument comes from upstream.lock.
#
#   bash scripts/build-image.sh                  # build stalwart-server:<RELEASE> into local docker
#   bash scripts/build-image.sh --push IMAGE     # also push IMAGE:<RELEASE>, and print its digest
#
# LINUX/AMD64 ON ANY HOST. The Dockerfile's compile stages run on the BUILD platform and
# cross-compile to x86_64, so an arm64 machine compiles natively and only the runtime stage's few
# RUN steps are emulated. That is checked rather than assumed: the build is refused unless the
# server binary inside the image is an x86-64 ELF.
#
# WHICH BUILDER. Whatever `docker buildx` would use by default, or the one BUILDX_BUILDER names.
# A pushed image is checked below by running it, which pulls it when the builder did not load it.
#
# MEMORY. The last step, linking the server with thin LTO across every crate, needs more than
# 3.8 GB on its own, so a builder capped below that (a small runner, or a BuildKit daemon with
# a fixed memory limit) is killed with SIGKILL at the end of the build. README.md ("Building")
# has the measured figures.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
usage() { echo "usage: bash scripts/build-image.sh [--push IMAGE]" >&2; exit 2; }

image="stalwart-server"
output=--load
case "${1:-}" in
  "") ;;
  --push) [ -n "${2:-}" ] || usage; image="$2"; output=--push ;;
  *) usage ;;
esac

# The gate, the pinned upstream tree, the patches; exports OUTPUT_* (see ci-prepare.sh).
. "$ROOT/scripts/ci-prepare.sh"

ref="$image:$OUTPUT_IMAGE_TAG"
meta="$(mktemp)"
trap 'rm -f "$meta"' EXIT

# No provenance attestation: the image's labels and /usr/share/doc/stalwart/SOURCE already name
# the source and revision, and an attestation records details of the machine that built it.
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --file "$ROOT/src/Dockerfile.stalwartserver" \
  --build-arg CHEF_IMAGE="$OUTPUT_CHEF_IMAGE" \
  --build-arg RUNTIME_IMAGE="$OUTPUT_RUNTIME_IMAGE" \
  --build-arg CARGO_FEATURES="$OUTPUT_CARGO_FEATURES" \
  --build-arg BUILD_JOBS="$OUTPUT_BUILD_JOBS" \
  --build-arg SOURCE_URL=https://github.com/kempu/StalwartServer \
  --build-arg SOURCE_REVISION="$OUTPUT_SOURCE_REVISION" \
  --build-arg RELEASE="$OUTPUT_IMAGE_TAG" \
  --build-arg SEARCH_INDEX_FORMAT="$OUTPUT_SEARCH_INDEX_FORMAT" \
  --metadata-file "$meta" \
  "$output" \
  --tag "$ref" \
  "$ROOT/src"

# The binary must be x86-64: bytes 18-19 of an ELF header are e_machine, 3e 00 for x86-64.
machine="$(docker run --rm --platform linux/amd64 --entrypoint /usr/bin/od "$ref" -An -tx1 -j18 -N2 /usr/local/bin/stalwart | tr -d ' \n')"
[ "$machine" = "3e00" ] || { echo "the server binary in $ref is not x86-64 (e_machine $machine) — refusing" >&2; exit 1; }
echo "built $ref: linux/amd64, server binary x86-64"

if [ "$output" = --push ]; then
  digest="$(sed -n 's/.*"containerimage\.digest": *"\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$meta" | head -1)"
  [ -n "$digest" ] || { echo "the build reported no digest for $ref" >&2; exit 1; }
  echo "pushed $ref@$digest"
fi
