# Changelog

## 0.4.0 — M3 the hands
- invariant gates land first, per house doctrine: coexistence check (systemd-oomd/earlyoom, read-only `systemctl is-active`) prepended as a warning to every action verb's output; kill gate — the CLI requires a fresh interactive TTY confirmation typing the target's exact pid back (no `--yes`, no env bypass, non-TTY stdin refuses outright), the daemon independently re-validates `(pid, starttime)` so a stale/reused pid can't slip through even if the CLI layer were bypassed; pid 1, kernel threads, and the daemon itself are never valid targets
- `calm <pid|comm> [--high SIZE|--release|--nice N|--kill]`: target resolution by pid or exact comm (ambiguous comm matches refuse with the candidate list); `--high` writes cgroup v2 `memory.high` with a floor computed from the target's own RSS (PLAN.md Invariant #2 — a size can never be set low enough to instant-thrash-OOM the thing it's meant to calm) and clamped to `[64M, MemTotal]`; `--release` clears to max; `--nice` reniced 0..19 (calm only ever lowers priority); `--kill` sends SIGTERM then an optional SIGKILL after a 5s live-check, TTY-confirmed each time. Every action ledgered to `RAMSTEIN_STATE_DIR/ledger.jsonl`
- `oom`: read-only risk snapshot (available/PSI/burn/ETA) plus the top-5 kill candidates by the kernel's own `/proc/<pid>/oom_score` — "who dies first if this goes bad"
- `advise`: six read-only nudge rules — sustained PSI (avg60), a fast RSS grower over the recent ring (MiB/h, needs ≥5min of real span to avoid extrapolation noise), swap >50% full (names the top tenant), unreaped zombies (names the negligent parent), another OOM-fighter active (stand-down language), ETA-to-OOM under an hour (points at `oom`)
- tests/smoke.sh: oom shape assert, advise's zombie + coexistence rules (via a fake `systemctl` shim), `calm --nice` unprivileged success, `calm --high`/`--release` against a fake cgroup tree (`RAMSTEIN_CGROUP_ROOT`, honored only when non-root), the kill gate's daemon-side stale-pid rejection and CLI-side non-TTY refusal, hostile input

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
