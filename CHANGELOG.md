# Changelog

## 0.6.0 — adopt the sutra backbone (behavior-preserving)
- vendored bin/sutra.py + bin/sutra.version (sutra 0.1.0, ByeByte is the pilot extraction); ramsteind/ramstein now import it as a sibling instead of hand-rolling the same skeleton
- ramsteind: load_config -> sutra.load_config; write_status -> sutra.write_status; the EWMA inline in poll_memory -> sutra.ewma_rate (fed `total - avail`, the used-equivalent whose increase is the burn — mathematically identical to the old avail-falling calc); the Control class deleted in favor of a dispatch closure over cfg/get_status carrying the unchanged domain commands (top/blame/swap/zombies/resolve/oom/advise/calm/kill), served by sutra.ControlServer + allow_uids({0, os.getuid(), owner_uid}) — ping/status are sutra's job now, and the M4 listen(64) fix comes along for free as sutra's own default
- ramstein: request()/fetch() now call sutra.request / sutra.read_status instead of hand-rolling the socket client and status.json fallback
- make check-sutra: verifies bin/sutra.py's sha256 against bin/sutra.version (integrity, always) and diffs against ~/code/REPOS/sutra/sutra.py when that checkout is present (freshness); wired into CI and the front of make smoke; make deb now ships bin/sutra.py alongside the bins
- no observable change: same socket contract, same status.json shape, same config semantics — make smoke + make attack stay green throughout

## 0.5.0 — M4 completion
- man/ramstein.1, man/ramsteind.8: groff -man source, verbs with real-output examples, config keys + clamps table, security model (kill gate, memory.high floor, coexistence, hostile-input doctrine) — installed by install.sh, removed by uninstall.sh
- make deb: minimal dpkg-deb package (bins to /usr/bin, units, man pages, config.json as a conffile); postinst/prerm/postrm share the owner_uid seed logic with install.sh via scripts/seed-owner-uid.py; never installed by smoke, only built and inspected
- hardening: systemd unit gets CapabilityBoundingSet (CAP_SYS_PTRACE, CAP_SYS_NICE, CAP_KILL, CAP_DAC_OVERRIDE, CAP_CHOWN — each mapped to a real code path), SystemCallFilter=@system-service, ProtectKernelTunables, ProtectClock, MemoryDenyWriteExecute, RestrictAddressFamilies=AF_UNIX; ProtectKernelTunables makes /sys read-only, which would have silently broken `calm --high`'s cgroup memory.high write — carved out via ReadWritePaths, verified against a LIVE calm --nice/--high/kill on a real fixture process (not just smoke fixtures) before calling it done; systemd-analyze verify clean, security score 4.7 OK
- tests/attack_socket.py: standalone adversarial harness covering the full M2/M3 command surface plus oversized/garbage/invalid-utf8/nested/unknown/rapid-reconnect/half-open-stall; make attack wired into CI alongside make smoke. Found a real bug: listen(4)'s backlog was too small for a rapid-reconnect burst (EAGAIN under 200 back-to-back connects) — bumped to listen(64), matching sutra's own documented rationale

## 0.4.1 — pill catches up
- extension/ramstein@asuramaya: fixed a swap-row mislabel — "X of Y free" reads like X is *used* (the "3 of 10" idiom), backwards for X being what's *left*; now "X free of Y", matching the CLI
- the pill was still M0-era: memory/swap/pressure/burn only, blind to everything M2/M3 unlocked. ramsteind now computes a small digest (top RSS process, zombie count, the single most-urgent advise line) on the sampler's own cadence and rides it along in status.json's new `pill` field — no socket client added to the pill, still one file + one GFileMonitor. New rows: top process (when available), zombies (only when >0), and an advise headline (only when there's something to say, with a "+N more" count)

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
