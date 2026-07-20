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

# ---- root, checked FIRST, before anything else --------------------------
# Fail fast and plainly rather than self-elevating.
if [[ $EUID -ne 0 ]]; then
  cat >&2 <<'EOF'
RAMstein needs root to install (binaries, systemd units). Re-run with sudo:

  sudo ./install.sh        (or: sudo make install)
EOF
  exit 1
fi

# No verified-release bootstrap yet: RAMstein has no published releases, so a
# `curl | sudo bash` one-liner could only ever fetch UNVERIFIED main-branch
# code — and the family rule is fail-closed: never install what can't be
# verified. Until the first release ships (with checksums, coldspot-style),
# this installer only runs from a checkout, next to the files it installs.
[[ -f "$SRC/bin/ramsteind" ]] || {
  echo "run install.sh from a RAMstein checkout:" >&2
  echo "  git clone https://github.com/asuramaya/RAMstein && cd RAMstein && sudo ./install.sh" >&2
  exit 1
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
VERSION="$(tr -d '[:space:]' < "$SRC/VERSION" 2>/dev/null || echo unknown)"

echo "== RAMstein ${VERSION} installer =="

# 1. binaries + version marker
echo "-- binaries -> $BINDIR"
for b in ramstein ramsteind ramstein-healthcheck ramstein-update; do
  install -m 0755 -o root -g root "$SRC/bin/$b" "$BINDIR/$b"
done
install -d -m 0755 "$SHAREDIR"
install -m 0644 "$SRC/VERSION" "$SHAREDIR/VERSION"

# 1b. man pages
echo "-- man pages -> $PREFIX/share/man"
install -d -m 0755 "$PREFIX/share/man/man1" "$PREFIX/share/man/man8"
install -m 0644 "$SRC/man/ramstein.1"  "$PREFIX/share/man/man1/ramstein.1"
install -m 0644 "$SRC/man/ramsteind.8" "$PREFIX/share/man/man8/ramsteind.8"

# 2. default config — the seed, never the master, and NEVER overwritten: a
# reinstall keeps your tuned copy. owner_uid is stamped to the installing
# user so the socket/status handoff points at the right account.
if [[ ! -f "$CONFDIR/config.json" ]]; then
  echo "-- config -> $CONFDIR/config.json (owner_uid=$OWNER_UID)"
  install -d -m 0755 "$CONFDIR"
  # shared with the .deb's postinst (scripts/seed-owner-uid.py) so the two
  # installers can't drift on what "seeding" means
  python3 "$SRC/scripts/seed-owner-uid.py" \
    "$SRC/config/config.json" "$CONFDIR/config.json" "$OWNER_UID"
  chown root:root "$CONFDIR/config.json"
  chmod 0644 "$CONFDIR/config.json"
else
  echo "-- config: keeping existing $CONFDIR/config.json (never overwritten)"
fi

# 3. systemd: daemon (+ updater/autocalm units, installed but NOT enabled)
echo "-- systemd units + enabling"
install -m 0644 "$SRC/systemd/system/ramsteind.service"        "$UNITDIR/ramsteind.service"
install -m 0644 "$SRC/systemd/system/ramstein-update.service"  "$UNITDIR/ramstein-update.service"
install -m 0644 "$SRC/systemd/system/ramstein-update.timer"    "$UNITDIR/ramstein-update.timer"
install -m 0644 "$SRC/systemd/system/ramstein-autocalm.service" "$UNITDIR/ramstein-autocalm.service"
install -m 0644 "$SRC/systemd/system/ramstein-autocalm.timer"   "$UNITDIR/ramstein-autocalm.timer"
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
config (auto_calm_enabled), runtime (ramstein autocalm arm, always resets
to dry-run on restart), and this timer. It only ever renices/squeezes; kill
always stays a human verb with a TTY confirm. Opt in, once all three matter:
  ramstein autocalm arm && sudo systemctl enable --now ramstein-autocalm.timer

>>> step 2 — the GNOME pill (per-account, as yourself, no sudo): <<<
    make pill && gnome-extensions enable ramstein@asuramaya
EOF
