# Changelog

## 0.3.0 — M2 per-process index
- ramsteind: per-process sampler (`/proc/[0-9]*/status` + `stat`) on its own cadence (`sample_every`), sqlite ring index at `RAMSTEIN_STATE_DIR/index.db` (WAL, `WITHOUT ROWID`) — `recent_ring` (~1h, every sample) + `hourly_ring` (~7d, one promoted sample/hour), identity key `(pid, starttime)` to survive pid reuse
- socket + CLI verbs go live, replacing the M0 stubs: `top` (RSS/swap ranked, `--swap`/`--limit`), `blame --since` (RSS deltas: grown/new/gone), `swap` (VmSwap occupants), `zombies` (live `/proc` scan, parent attribution — not the index, zombies are now-questions)
- tests/smoke.sh: M2 fixture coverage — a 100MiB allocator ranked in `top` and seen growing in `blame`, a real fork/reap zombie lifecycle, hostile-input rejections, sampler perf canary (<500ms/pass)

## 0.2.0 — M1 pill
- extension/ramstein@asuramaya: Quick Settings pill — available memory + ETA-to-OOM on the tile, heats on warn/hot; expanded: alert banner (psi full / available / ETA), memory + swap + pressure + burn rows, version footer; event-driven via GFileMonitor with a 60s fallback tick
- make pill: user-level install target (never root)

## 0.1.0 — Wave 1 packaging
- install.sh / uninstall.sh: root two-step installer (daemon now, pill arrives with M1); never overwrites /etc/ramstein/config.json, seeds owner_uid from $SUDO_UID; uninstall keeps /etc/ramstein + /var/lib/ramstein unless --purge
- ramstein-healthcheck: one-line vitals verdict — status.json fresh (< 3× declared poll_interval) + socket ping ok, exit 0/nonzero
- ramstein-update: --check (+ --json) against GitHub releases, graceful before any release exists; daily notify-only timer (installed, not enabled); install path stays an explicit stub until releases exist
- systemd: ramstein-update.timer/.service (daily --check, DynamicUser)
- CI: py_compile, bash -n, shellcheck, make smoke
- community files: CODE_OF_CONDUCT, CONTRIBUTING, SECURITY

## 0.0.1 — M0 truth engine
- ramsteind: /proc/meminfo + /proc/pressure/memory polling, EWMA burn rate of available-memory consumption, ETA-to-OOM, status.json, hardened control socket
- ramstein: status verb (human + --json)
- make smoke: shape + hostile-input assertions
