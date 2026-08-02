#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 asuramaya and RAMstein contributors
# RAMstein installer — the memory demon: daemon, CLI, healthcheck, updater,
# systemd units. Root-only, and ONLY root-only: this script never re-execs
# itself under sudo (family rule — a script that quietly escalates itself is
# exactly what once misattributed the human user to "root"; see coldspot's
# git log). If you're not root, it says so and stops; you always type sudo
# yourself, exactly once, so there is no ambiguity about who actually ran it.
# Install is TWO deliberate steps split by privilege: this (root) installs
# the daemon; the GNOME pill is a separate, per-account, non-root step — it
# arrives with M1, and installing a file into your own home never needed
# root in the first place.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
PREFIX="${PREFIX:-/usr/local}"
BINDIR="$PREFIX/bin"
SHAREDIR="$PREFIX/share/ramstein"
UNITDIR="/etc/systemd/system"
CONFDIR="/etc/ramstein"

# principal = WHO (RAMstein's stable identity); namespace = WHAT-FOR (what
# this signature authorizes). Never conflate the two — see ~/code/REPOS/RELEASE.md.
REPO_SLUG="asuramaya/RAMstein"
SIGN_PRINCIPAL="ramstein"
SIGN_NAMESPACE="ramstein-release"

# Trust anchor for the curl-pipe-bash bootstrap below, EMBEDDED directly:
# `curl -fsSL .../install.sh | sudo bash` fetches ONLY this file — not the
# sibling release-signing/ directory — so the anchor has to travel embedded
# in whichever copy of this script is currently executing, not be read from
# a file that hasn't been fetched yet (that would mean trusting the very
# release being verified). Kept in sync with
# packaging/release-signing/allowed_signers by `make sync-signers` — never
# hand-edit this. Single-quoted deliberately: the value can span multiple
# lines (one per pinned key) and must never be shell-interpolated.
RELEASE_ALLOWED_SIGNERS='ramstein namespaces="ramstein-release,pills-tag" sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIAvqlv848gk9uzM40ZsFZTQeXsQKpxYaK4Fi8ubNl1H7AAAAFnNzaDphc3VyYW1heWEtbWFzdGVyLTE= ra-master-1
ramstein namespaces="ramstein-release,pills-tag" sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIFJBKAsk6b4YR2UH/UZ1Rk24PxepTYNkF7zflo01AmlZAAAAFnNzaDphc3VyYW1heWEtbWFzdGVyLTI= ra-master-2
ramstein namespaces="ramstein-release,pills-tag" sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIHTVqgo3ARbpTq04YlQksobfGIbBAw21nbE6HyeCPgxBAAAAFnNzaDphc3VyYW1heWEtbWFzdGVyLTM= ra-master-3
ramstein namespaces="ramstein-release,pills-tag" sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIP15ZeSJYWryHN2WEHDlJbWk/vA+j5JFgb9RSzT1SHveAAAAFnNzaDphc3VyYW1heWEtbWFzdGVyLTQ= ra-master-4
'

# Has a real key been provisioned, or is this still the empty placeholder?
has_signing_key() {
  [[ -n "${RELEASE_ALLOWED_SIGNERS// /}" ]]
}

# True/false: does the detached signature in $2 (a file path) verify $1 (a
# file path, the exact bytes that were signed) against the pinned key(s) in
# $3 (an allowed_signers path), for principal $4 and namespace $5? Principal
# is WHO (RAMstein's identity); namespace is WHAT-FOR (what this signature
# authorizes) — a signature made for a different purpose, or by a principal
# not pinned in allowed_signers, must not verify here.
verify_signature() {
  local data_file="$1" sig_file="$2" signers="$3" principal="$4" ns="$5"
  ssh-keygen -Y verify -f "${signers}" -I "${principal}" -n "${ns}" -s "${sig_file}" \
    < "${data_file}" >/dev/null 2>&1
}

# tests/test_signing.sh sources this file (with RAMSTEIN_INSTALL_SOURCED=1) to
# reach has_signing_key/verify_signature directly, without running an actual
# install — everything above this point only ever DEFINES functions/vars, so
# returning here before the root check keeps sourcing side-effect-free.
if [[ "${RAMSTEIN_INSTALL_SOURCED:-0}" == "1" ]]; then
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi

# ---- root, checked FIRST, before anything else --------------------------
# Fail fast and plainly rather than self-elevating.
if [[ $EUID -ne 0 ]]; then
  cat >&2 <<'EOF'
RAMstein needs root to install (binaries, systemd units). Re-run with sudo:

  sudo ./install.sh        (or: sudo make install)
EOF
  exit 1
fi

# Verified-release bootstrap (docs/RELEASE-SIGNING.md): v0.11.1 was
# RAMstein's first release published with SHA256SUMS (2026-08-01), so
# `curl -fsSL .../install.sh | sudo bash` — running with no sibling files
# next to it — now has something real to verify against, instead of only
# ever being able to fetch unverified main-branch code. Fetches the
# published .deb + SHA256SUMS (+ SHA256SUMS.sig once a key is provisioned)
# and dpkg -i's it — the .deb's own postinst does everything the rest of
# this script does for a checkout install (config seed, systemd
# enable+start), so nothing else needs to run afterward. Fails closed at
# every step: no SHA256SUMS, a checksum mismatch, or (once a key is pinned)
# a missing/invalid signature all abort rather than proceed.
bootstrap_from_release() {
  command -v curl >/dev/null 2>&1 || { echo "curl is required for remote install" >&2; exit 1; }
  command -v dpkg >/dev/null 2>&1 || {
    echo "dpkg not found — this quick-install path needs a Debian/Ubuntu" >&2
    echo "system. Clone the repo and run install.sh from a checkout instead." >&2
    exit 1
  }

  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "${tmp}"' EXIT
  local base="https://github.com/${REPO_SLUG}/releases/latest/download"

  echo "== fetching latest RAMstein release =="
  curl -fsSL "${base}/SHA256SUMS" -o "${tmp}/SHA256SUMS" \
    || { echo "could not fetch the release checksum manifest (SHA256SUMS) — refusing to install unverified." >&2; exit 1; }
  local debname
  debname="$(awk '$2 ~ /_all\.deb$/ { n = $2; sub(/^\*/, "", n); print n; exit }' "${tmp}/SHA256SUMS")"
  [[ -n "${debname}" ]] || { echo "SHA256SUMS has no .deb entry — aborting." >&2; exit 1; }
  curl -fsSL "${base}/${debname}" -o "${tmp}/${debname}" \
    || { echo "deb download failed" >&2; exit 1; }

  local want got
  want="$(awk -v n="${debname}" '$2 == n || $2 == "*"n { print $1; exit }' "${tmp}/SHA256SUMS")"
  got="$(sha256sum "${tmp}/${debname}" | cut -d' ' -f1)"
  [[ "${want}" == "${got}" ]] || { echo "CHECKSUM MISMATCH for ${debname} — aborting." >&2; exit 1; }
  echo "sha256 verified."

  if has_signing_key; then
    curl -fsSL "${base}/SHA256SUMS.sig" -o "${tmp}/SHA256SUMS.sig" \
      || { echo "signing key is pinned but the release has no SHA256SUMS.sig — refusing to install unsigned." >&2; exit 1; }
    printf '%s\n' "${RELEASE_ALLOWED_SIGNERS}" > "${tmp}/allowed_signers"
    verify_signature "${tmp}/SHA256SUMS" "${tmp}/SHA256SUMS.sig" "${tmp}/allowed_signers" \
      "${SIGN_PRINCIPAL}" "${SIGN_NAMESPACE}" \
      || { echo "SIGNATURE VERIFICATION FAILED — aborting." >&2; exit 1; }
    echo "signature verified."
  else
    echo "warning: no release-signing key provisioned yet (see docs/RELEASE-SIGNING.md) — proceeding on SHA256 alone." >&2
  fi

  dpkg -i "${tmp}/${debname}"
  echo
  echo ">>> step 2 — the GNOME pill (per-account, as yourself, no sudo): <<<"
  echo "    this quick-install path doesn't package the pill yet — clone the"
  echo "    repo and run 'make pill' from a checkout for the Quick Settings"
  echo "    extension (gnome-extensions enable ramstein@asuramaya afterward)."
}

[[ -f "$SRC/src/bin/ramsteind" ]] || {
  bootstrap_from_release
  exit 0
}

# The one thing that needs to know about a human account: status.json and the
# control socket are chowned to owner_uid so the CLI and (come M1) the pill
# can read them without root. Since this script never sudos itself, $SUDO_UID
# is reliable here — it's set by the single sudo call the human actually
# typed. From a plain root shell there's no such hint; fall back to 1000 and
# say so.
OWNER_UID="${SUDO_UID:-}"
if [[ -z "$OWNER_UID" ]]; then
  OWNER_UID=1000
  echo "note: no \$SUDO_UID (plain root shell?) — seeding owner_uid=1000;" \
       "edit $CONFDIR/config.json if your account differs."
fi
VERSION="$(tr -d '[:space:]' < "$SRC/packaging/VERSION" 2>/dev/null || echo unknown)"

echo "== RAMstein ${VERSION} installer =="

# 1. binaries + version marker
echo "-- binaries -> $BINDIR"
for b in ramstein ramsteind ramstein-healthcheck ramstein-update; do
  install -m 0755 -o root -g root "$SRC/src/bin/$b" "$BINDIR/$b"
done
# Sutra install-path adoption (BOOTSTRAP.md, ruling 3e44bd95): every pill
# vendoring sutra.py into the SAME shared bin dir under the same name made
# any two pills installed together collide: dpkg refuses the second
# package outright; a plain `install` here has no ownership tracking and
# would silently overwrite, anchors included. Measured, not theorised: on
# the operator's own machine two different pills' sutra.py were already
# two different canonical commits, with no anchor in that shared dir to
# catch it. The vendored copies now live in their own private,
# package-owned dir instead of beside the binaries; every binary that
# imports sutra finds it there via a small sys.path bootstrap preamble
# instead of relying on co-location. Anchors travel with the code so a
# post-install check can verify the INSTALLED copy, not just the repo one.
echo "-- sutra commons -> $SHAREDIR/lib"
install -d -m 0755 "$SHAREDIR/lib"
for f in sutra.py sutra.version sutra.commit \
         sutra_update.py sutra_update.version sutra_update.commit \
         sutra_xen.py sutra_xen.version sutra_xen.commit; do
  [ -f "$SRC/src/share/ramstein/lib/$f" ] && install -m 0644 -o root -g root "$SRC/src/share/ramstein/lib/$f" "$SHAREDIR/lib/$f"
done
# Old-layout leftovers: a machine that ran a pre-adoption install.sh has
# these sitting in $BINDIR, owned by nothing (a .deb upgrade drops
# package-owned files automatically on its own; install.sh's copies never
# were package-owned by anything). Left behind they'd linger forever, so
# clean them up unconditionally on every install, not just an upgrade.
for f in sutra.py sutra.version sutra.commit \
         sutra_update.py sutra_update.version sutra_update.commit \
         sutra_xen.py sutra_xen.version sutra_xen.commit; do
  rm -f "$BINDIR/$f"
done
install -d -m 0755 "$SHAREDIR"
install -m 0644 "$SRC/packaging/VERSION" "$SHAREDIR/VERSION"
# Wave B M5: a persistent copy of the signing anchor at the installed
# prefix — ramstein-update's anchor_candidates looks here (not the
# repo-relative path, which only resolves for a dev checkout run in place).
# Ships empty until the operator's first sync-signers ceremony; re-installing
# after that point re-copies whatever the checkout's anchor says at the time.
install -m 0644 "$SRC/packaging/release-signing/allowed_signers" "$SHAREDIR/allowed_signers"

# 1b. man pages
echo "-- man pages -> $PREFIX/share/man"
install -d -m 0755 "$PREFIX/share/man/man1" "$PREFIX/share/man/man8"
install -m 0644 "$SRC/src/data/man/man1/ramstein.1"  "$PREFIX/share/man/man1/ramstein.1"
install -m 0644 "$SRC/src/data/man/man8/ramsteind.8" "$PREFIX/share/man/man8/ramsteind.8"

# 2. default config — the seed, never the master, and NEVER overwritten: a
# reinstall keeps your tuned copy. owner_uid is stamped to the installing
# user so the socket/status handoff points at the right account.
if [[ ! -f "$CONFDIR/config.json" ]]; then
  echo "-- config -> $CONFDIR/config.json (owner_uid=$OWNER_UID)"
  install -d -m 0755 "$CONFDIR"
  # shared with the .deb's postinst (packaging/seed-owner-uid.py) so the two
  # installers can't drift on what "seeding" means
  python3 "$SRC/packaging/seed-owner-uid.py" \
    "$SRC/src/data/config/config.json" "$CONFDIR/config.json" "$OWNER_UID"
  chown root:root "$CONFDIR/config.json"
  chmod 0644 "$CONFDIR/config.json"
else
  echo "-- config: keeping existing $CONFDIR/config.json (never overwritten)"
fi

# 3. systemd: daemon (+ updater/autocalm units, installed but NOT enabled)
echo "-- systemd units + enabling"
install -m 0644 "$SRC/src/data/systemd/system/ramsteind.service"        "$UNITDIR/ramsteind.service"
install -m 0644 "$SRC/src/data/systemd/system/ramstein-update.service"  "$UNITDIR/ramstein-update.service"
install -m 0644 "$SRC/src/data/systemd/system/ramstein-update.timer"    "$UNITDIR/ramstein-update.timer"
install -m 0644 "$SRC/src/data/systemd/system/ramstein-autocalm.service" "$UNITDIR/ramstein-autocalm.service"
install -m 0644 "$SRC/src/data/systemd/system/ramstein-autocalm.timer"   "$UNITDIR/ramstein-autocalm.timer"
systemctl daemon-reload
systemctl enable ramsteind.service
# `enable --now` on an ALREADY-active unit is a no-op start — it would leave
# the old binary running in memory even though we just overwrote it on disk.
# Detect a re-install and explicitly restart so the new daemon (and any
# unit-file changes) actually take effect.
if systemctl is-active --quiet ramsteind.service; then
  echo "-- restarting ramsteind to load the updated daemon"
  systemctl restart ramsteind.service
else
  systemctl start ramsteind.service
fi
# The daily update timer only ever CHECKS (notify-only, unprivileged) — but
# even a check that phones GitHub is opt-in, family-wide. Enable deliberately
# (see the post-install note).

# 4. verify perms
echo "-- verifying"
verify() { local got; got="$(stat -c '%a' "$1" 2>/dev/null || echo '?')"
  [[ "$got" == "$2" ]] && echo "   OK   $1 ($got)" || echo "   WARN $1 is $got, expected $2"; }
verify "$BINDIR/ramsteind" 755
verify "$CONFDIR/config.json" 644

cat <<EOF

== RAMstein ${VERSION} installed ==
  ramstein status             available memory, PSI, burn rate, ETA-to-OOM
  ramstein-healthcheck        one-line vitals verdict (exit 0 = healthy)
  ramstein-update --check     is a newer release out? (never installs by itself)
  man ramstein / man 8 ramsteind   full verb reference, config keys, security model
  Remove:  sudo ./uninstall.sh   (keeps /etc/ramstein + /var/lib/ramstein; --purge drops them)

daily update CHECK is off by default (it's notify-only, never installs). Opt in:
  sudo systemctl enable --now ramstein-update.timer

auto-calm is off by default and stays off across THREE separate gates —
config (auto_calm_enabled), runtime (ramstein autocalm arm — persists
across restarts once armed, disarm any time with ramstein autocalm dry),
and this timer. It only ever renices/squeezes; kill always stays a human
verb with a TTY confirm. Opt in, once all three matter:
  ramstein autocalm arm && sudo systemctl enable --now ramstein-autocalm.timer

>>> step 2 — the GNOME pill (per-account, as yourself, no sudo): <<<
    make pill && gnome-extensions enable ramstein@asuramaya
EOF
