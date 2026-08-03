#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 asuramaya and ramstein contributors
# ramstein uninstaller. Keeps /etc/ramstein (config) and /var/lib/ramstein
# (state) unless --purge is given. Root-only, and never self-elevates — see
# install.sh for why. The GNOME pill is a per-account, non-root install
# (make pill) — removing it stays a per-account step, never this script's
# job: gnome-extensions disable/uninstall ramstein@asuramaya, yourself.
set -uo pipefail

PREFIX="${PREFIX:-/usr/local}"
BINDIR="$PREFIX/bin"
SHAREDIR="$PREFIX/share/ramstein"
UNITDIR="/etc/systemd/system"
PURGE=0

for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    -h|--help) echo "usage: ./uninstall.sh [--purge]"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "ramstein uninstaller needs root — run: sudo ./uninstall.sh" >&2
  exit 1
fi

echo "== ramstein uninstaller =="

echo "-- stopping service + timer"
# var-lib-ramstein-extra.img.swap first, and explicitly -- `disable --now`
# is what actually swapoffs it; skipping this would leave the swap file
# active at the kernel level even after ramsteind itself stops, and a
# later --purge's `rm -rf /var/lib/ramstein` would unlink it out from
# under still-active swap instead of cleanly turning it off first.
systemctl disable --now var-lib-ramstein-extra.img.swap ramsteind.service \
  ramstein-update.timer ramstein-update.service \
  ramstein-autocalm.timer ramstein-autocalm.service 2>/dev/null || true

echo "-- removing files"
for b in ramstein ramsteind ramstein-healthcheck ramstein-update; do
  rm -f "$BINDIR/$b"
done
# Legacy cleanup: sutra used to live beside the binaries in $BINDIR
# (pre-BOOTSTRAP.md). The current install.sh no longer writes there and
# already removes any it finds on every install, but an uninstall can run
# against a machine that was never reinstalled since, so clean up here too.
for f in sutra.py sutra.version sutra.commit \
         sutra_update.py sutra_update.version sutra_update.commit \
         sutra_xen.py sutra_xen.version sutra_xen.commit; do
  rm -f "$BINDIR/$f"
done
rm -f "$UNITDIR/ramsteind.service" "$UNITDIR/ramstein-update.service" "$UNITDIR/ramstein-update.timer" \
      "$UNITDIR/ramstein-autocalm.service" "$UNITDIR/ramstein-autocalm.timer" \
      "$UNITDIR/var-lib-ramstein-extra.img.swap"
# Covers the current sutra location too: $SHAREDIR/lib/ (BOOTSTRAP.md).
rm -rf "$SHAREDIR"
rm -f "$PREFIX/share/man/man1/ramstein.1" "$PREFIX/share/man/man8/ramsteind.8"
systemctl daemon-reload

if [[ "$PURGE" -eq 1 ]]; then
  echo "-- purging config + state"
  rm -rf /etc/ramstein /var/lib/ramstein
  echo "ramstein fully removed."
else
  echo "ramstein removed. (kept /etc/ramstein and /var/lib/ramstein — use --purge to drop them.)"
fi
