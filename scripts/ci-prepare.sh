#!/usr/bin/env bash
# Prepare a build: run the publish gate, fetch the exact upstream commit upstream.lock pins,
# apply patches/ in order, and put the Dockerfile into that tree, which is the build context.
#
# The build pipeline SOURCES this (`. scripts/ci-prepare.sh`) and reads the OUTPUT_* exports
# as run variables, so no version is restated in the pipeline. Run by hand (bash
# scripts/ci-prepare.sh) it prepares ./src for a local build; see README.md ("Building").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../upstream.lock
. "$ROOT/upstream.lock"

for var in STALWART_REPO STALWART_TAG STALWART_COMMIT RELEASE SEARCH_INDEX_FORMAT CHEF_IMAGE RUNTIME_IMAGE CARGO_FEATURES BUILD_JOBS; do
  [ -n "${!var:-}" ] || { echo "upstream.lock sets no $var" >&2; exit 1; }
done
case "$RELEASE" in
  "$STALWART_TAG"-kempu.[0-9]*) ;;
  *) echo "RELEASE '$RELEASE' must be '$STALWART_TAG-kempu.<n>'" >&2; exit 1 ;;
esac

bash "$ROOT/scripts/check-hygiene.sh"

SRC="$ROOT/src"
rm -rf "$SRC"
git clone --quiet --depth 1 --branch "$STALWART_TAG" "$STALWART_REPO" "$SRC"
got="$(git -C "$SRC" rev-parse HEAD)"
[ "$got" = "$STALWART_COMMIT" ] || {
  echo "upstream tag $STALWART_TAG is $got, not the $STALWART_COMMIT upstream.lock pins — refusing" >&2
  exit 1
}

shopt -s nullglob
patches=("$ROOT"/patches/*.patch)
[ "${#patches[@]}" -gt 0 ] || { echo "no patches in patches/ — nothing to build that upstream does not already ship" >&2; exit 1; }
for patch in "${patches[@]}"; do
  git -C "$SRC" apply --check "$patch" || { echo "$(basename "$patch") no longer applies to $STALWART_TAG" >&2; exit 1; }
  git -C "$SRC" apply "$patch"
  echo "applied $(basename "$patch")"
done

cp "$ROOT/Dockerfile" "$SRC/Dockerfile.stalwartserver"

export OUTPUT_IMAGE_TAG="$RELEASE"
OUTPUT_SOURCE_REVISION="$(git -C "$ROOT" rev-parse HEAD)"
export OUTPUT_SOURCE_REVISION
export OUTPUT_CHEF_IMAGE="$CHEF_IMAGE"
export OUTPUT_RUNTIME_IMAGE="$RUNTIME_IMAGE"
export OUTPUT_CARGO_FEATURES="$CARGO_FEATURES"
export OUTPUT_BUILD_JOBS="$BUILD_JOBS"
export OUTPUT_SEARCH_INDEX_FORMAT="$SEARCH_INDEX_FORMAT"
echo "prepared $RELEASE: upstream $STALWART_TAG ($STALWART_COMMIT) + ${#patches[@]} patch(es), revision $OUTPUT_SOURCE_REVISION"
