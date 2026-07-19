#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 asuramaya and RAMstein contributors
# RAMstein uninstaller. Keeps /etc/ramstein (config) and /var/lib/ramstein
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
  echo "RAMstein uninstaller needs root — run: sudo ./uninstall.sh" >&2
  exit 1
fi

echo "== RAMstein uninstaller =="

echo "-- stopping service + timer"
systemctl disable --now ramsteind.service ramstein-update.timer ramstein-update.service 2>/dev/null || true

echo "-- removing files"
for b in ramstein ramsteind ramstein-healthcheck ramstein-update; do
  rm -f "$BINDIR/$b"
done
rm -f "$UNITDIR/ramsteind.service" "$UNITDIR/ramstein-update.service" "$UNITDIR/ramstein-update.timer"
rm -rf "$SHAREDIR"
rm -f "$PREFIX/share/man/man1/ramstein.1" "$PREFIX/share/man/man8/ramsteind.8"
systemctl daemon-reload

if [[ "$PURGE" -eq 1 ]]; then
  echo "-- purging config + state"
  rm -rf /etc/ramstein /var/lib/ramstein
  echo "RAMstein fully removed."
else
  echo "RAMstein removed. (kept /etc/ramstein and /var/lib/ramstein — use --purge to drop them.)"
fi
