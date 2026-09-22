#!/usr/bin/env bash
# The publish gate: refuse to build from a repository that would publish something it should not.
#
# This repository is public, and everything in it — every file, every commit message, every
# author line — is published with it. Three rules hold for all of it:
#
#   1. Commits are authored and committed under a GitHub no-reply address, never a personal
#      or work address.
#   2. No session or workspace links, which point somewhere nobody outside can open.
#   3. Every e-mail address a patch ADDS, and every address in the harness, is on a reserved
#      example domain (RFC 2606): example.com/.net/.org, their subdomains, or *.example.
#
# A fourth rule needs a list this repository must not carry: the names of the systems and the
# organisations the build runs for. That list is the HYGIENE_DENYLIST environment variable, an
# extended regular expression the build pipeline sets from its own private configuration. When
# it is set, nothing tracked here and no commit message may match it. Unset, rules 1-3 still run.
#
# Exits non-zero naming what broke, so the build stops before anything is built or pushed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
say() { echo "hygiene: $*" >&2; fail=1; }

# 1. Identities.
while IFS= read -r email; do
  case "$email" in
    *@users.noreply.github.com | noreply@github.com) ;;
    *) say "a commit is authored or committed as '$email' — use a GitHub no-reply address" ;;
  esac
done < <(git log --format='%ae%n%ce' | sort -u)

# 2. Links nobody outside can follow.
if git grep -n -I -E 'claude\.ai/code|Claude-Session:' -- . ':!scripts/check-hygiene.sh' >/dev/null; then
  say "a tracked file carries a session link:"; git grep -n -I -E 'claude\.ai/code|Claude-Session:' -- . ':!scripts/check-hygiene.sh' >&2
fi
if git log --format='%H %B' | grep -n -E 'claude\.ai/code|Claude-Session:' >/dev/null; then
  say "a commit message carries a session link"
fi

# 3. Addresses: only reserved example domains in what WE wrote.
reserved='@([a-z0-9-]+\.)*(example\.(com|net|org)|[a-z0-9-]+\.example)$'
{
  grep -h -E '^\+' patches/*.patch 2>/dev/null | grep -v -E '^\+\+\+' || true
  git ls-files harness | xargs -r cat
} | grep -o -i -E '[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}' | sort -u | while IFS= read -r addr; do
  if ! printf '%s' "$addr" | grep -q -i -E "$reserved"; then
    echo "hygiene: '$addr' is not on a reserved example domain" >&2
    echo x
  fi
done | grep -q x && fail=1

# 4. The private list.
if [ -n "${HYGIENE_DENYLIST:-}" ]; then
  if git grep -n -I -i -E "$HYGIENE_DENYLIST" >/dev/null; then
    say "a tracked file names something on the private list:"
    git grep -n -I -i -E "$HYGIENE_DENYLIST" >&2
  fi
  if git log --format='%h %an %ae%n%B' | grep -n -i -E "$HYGIENE_DENYLIST" >/dev/null; then
    say "a commit (message or author) names something on the private list:"
    git log --format='%h %an %ae%n%B' | grep -n -i -E "$HYGIENE_DENYLIST" >&2
  fi
else
  echo "hygiene: HYGIENE_DENYLIST is not set; the private-name check did not run" >&2
fi

[ "$fail" -eq 0 ] || { echo "hygiene: refusing to build" >&2; exit 1; }
echo "hygiene: clean"
