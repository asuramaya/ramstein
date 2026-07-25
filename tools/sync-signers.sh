#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# `make sync-signers` — rebuild release-signing/allowed_signers from the
# fleet's canonical pubkeys, per ~/code/REPOS/RELEASE.md's sync-signers
# doctrine.
#
# Canonical key home (operator ruling 13ee52ce): ~/.ssh/asuramaya-master/ —
# OUTSIDE every repo, never committed, never a sibling checkout. This is a
# LOCAL-ONLY act by construction: CI can never reach $HOME.
#
# ALWAYS a full rebuild, never an append: RA's first ceremony left keys
# unpinned across other repos by appending one at a time. Refuses to run
# unless it finds exactly 4 canonical keys, so a partial/broken key home
# can't silently produce a partial anchor.
#
# SEQUENCING: this populates the anchor. Run it ONLY in the same act as
# cutting the operator's first signed RAMstein release — arming
# release-signing/allowed_signers any earlier bricks `ramstein update`
# against every release published before the arming.
#
# Unlike kast/phanspeed, RAMstein's install.sh has no curl-pipe bootstrap
# (it deliberately only ever runs from a checkout — no verified-release
# fetch path exists yet, see install.sh's own header comment), so there is
# no embedded RELEASE_ALLOWED_SIGNERS twin to keep in sync here — this
# script only ever touches the one anchor file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)"
PRINCIPAL="ramstein"
NAMESPACES="ramstein-release,pills-tag"

KEY_HOME="${KEY_HOME:-$HOME/.ssh/asuramaya-master}"
if [[ ! -d "$KEY_HOME" ]]; then
  echo "ERROR: canonical key home not found at $KEY_HOME." >&2
  echo "       Set KEY_HOME=/path/to/asuramaya-master and retry." >&2
  exit 1
fi

mapfile -t pubs < <(find "$KEY_HOME" -maxdepth 1 -name '*.pub' | LC_ALL=C sort)
if [[ "${#pubs[@]}" -ne 4 ]]; then
  echo "ERROR: expected exactly 4 canonical pubkeys in $KEY_HOME, found ${#pubs[@]}." >&2
  echo "       Never partially sync — see RELEASE.md's sync-signers section." >&2
  exit 1
fi

anchor="$HERE/release-signing/allowed_signers"
tmp="$(mktemp)"
for p in "${pubs[@]}"; do
  printf '%s namespaces="%s" %s\n' "$PRINCIPAL" "$NAMESPACES" "$(cat "$p")"
done > "$tmp"
mv "$tmp" "$anchor"
echo "rebuilt $anchor from ${#pubs[@]} canonical keys ($KEY_HOME)"
echo "remember: install.sh / make deb ship a copy to the installed prefix on"
echo "the NEXT release build — re-run make deb / re-install after this."
