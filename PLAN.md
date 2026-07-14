# RAMstein — build plan

The memory sibling of the family: **kast · phanspeed · coldspot · byebyte ·
RAMstein**. coldspot governs the internet, phanspeed governs power, byebyte
governs storage — RAMstein governs **bytes alive**: process memory, RSS
growth, swap pressure, PSI, OOM risk, zombies, runaway processes. Its pill
shows available memory and an ETA-to-OOM under current pressure.

## Boundary charter (vs byebyte)

byebyte owns **bytes-at-rest** — every filesystem, including tmpfs: file
*usage* on a tmpfs mount and its quota headroom are disk-shaped accounting.
RAMstein owns **bytes-alive** — RSS, swap *contents*, PSI, PIDs: process-
shaped accounting, who's holding memory hostage right now. Two edge cases,
deliberately not deduplicated:

- **tmpfs** is memory-backed, so both tools see it. byebyte reports it
  `df`-shaped (bytes written, quota left); RAMstein reports it memory-shaped
  (it's paged like anonymous memory, competes for the same PSI). Same bytes,
  two dialects — "why is /tmp full" and "why is memory tight" want different
  sentences from different tools.
- **swap** splits down the middle. The swap *file's* disk footprint (size,
  growth, whether it's about to fill the partition) is byebyte's. Swap
  *occupancy* — who's actually parked in it, via `VmSwap` — is RAMstein's.
  byebyte can say the swapfile is 98% full; only RAMstein says who's in it.

## Constitution

- Free tool, not a product. GPLv3. Open source on GitHub; only home is
  asuramaya.com (portfolio).
- Same dream as every sibling: merged into the OS one day. Every decision
  survives a hypothetical Debian maintainer: stdlib-only Python, FHS-clean,
  man pages, no telemetry, `.deb`-able.
- House doctrine unchanged: a daemon that owns the truth, a verb CLI over it,
  a GNOME pill on top. State on disk is the seed, never the master.

## Anatomy

```
ramstein/
├── bin/
│   ├── ramsteind             # daemon — /proc+PSI+cgroup truth, gated privileged actions
│   ├── ramstein              # verb CLI, JSON over AF_UNIX control socket
│   ├── ramstein-healthcheck  # poller not wedged? status shape valid?
│   └── ramstein-update       # click-to-install only, never unattended
├── extension/                # ramstein@asuramaya — GNOME 50 Quick Settings pill
├── systemd/system/           # ramsteind.service (ProtectSystem=strict etc.) + update timer
├── config/                   # default config.json — seed, never master
├── tests/                    # attack_socket.py, /proc fixture trees, cgroup loopback rig
├── docs/ARCHITECTURE.md
├── Makefile                  # smoke / deploy / pill
├── install.sh                # sudo installs daemon; pill is its own user-level step
└── LICENSE (GPLv3) · VERSION · CHANGELOG.md · README.md
```

Unlike byebyte — root for nearly everything (statvfs across mounts, du-walks
under other users, purge) — most of what RAMstein reads is already world-
readable: `/proc/meminfo`, `/proc/pressure/memory`, and your own `/proc/<pid>/
status` need no privilege. Only *other users'* process internals, cgroup
`memory.high` writes, `renice`, and `kill` do. That argues for a leaner model
than byebyte's single root daemon — an unprivileged `ramsteind` serving
read-only verbs, with a narrow `CAP_SYS_NICE`+`CAP_KILL` helper (not full
root) invoked only for `calm`/kill. House doctrine argues the other way: one
daemon, one thing to secure. M0 ships single-daemon, for family consistency
and to inherit the hostile-input socket doctrine for free; the privilege
split is a documented candidate to revisit later, not a decision made here.

- IPC: newline-delimited JSON over `/run/ramstein/control.sock`, SO_PEERCRED-
  gated (root or owning UID), socket mode 0660 (phanspeed model, inherited).
- `status.json` at `/run/ramstein/status.json`, 0640. Pill reads it via
  Gio.FileMonitor — event-driven, no polling, never root.
- Index (M2+): sqlite (stdlib) at `/var/lib/ramstein/index.db` — byebyte's
  per-directory-aggregation pattern applied to per-pid/per-cgroup snapshots,
  ring-buffered for `blame --since`.

## Data sources

`/proc/meminfo` (MemAvailable/SwapFree, cheap, every tick) · `/proc/pressure/
memory` (PSI some/full × avg10/60/300, kernel-computed, needs `CONFIG_PSI` —
on by default for years) · `/proc/<pid>/status` (VmRSS/VmSwap/VmHWM, one read
per pid, the workhorse for `top`/`blame`/`swap` — note summed VmSwap won't
exactly equal `SwapTotal − SwapFree`, shared/CoW pages count per-holder, the
swap analogue of byebyte's df-vs-du honesty) · `/proc/<pid>/smaps_rollup`
(accurate PSS, shared pages divided fairly — an order of magnitude more
expensive than `status`, sampled on demand, never every tick) · cgroup v2
`memory.current`/`memory.pressure` (per-cgroup PSI, and the write target for
`calm`) · `oom_score`/`oom_score_adj` (what the kernel's own killer would
pick — grounds `oom`'s ranking in the kernel's own math, not a reinvention).

## Verbs

| verb | what |
|---|---|
| `ramstein status` | available memory, PSI, burn rate, ETA-to-OOM |
| `ramstein top` | live per-process RSS + swap — a memory-aware `top` |
| `ramstein blame [--since T]` | what GREW in RSS — join of two snapshots |
| `ramstein oom` | risk + ETA from PSI/burn; what oom_score would pick now |
| `ramstein swap` | who's parked in swap — VmSwap across `/proc/*/status` |
| `ramstein zombies` | defunct processes and the parent not reaping them |
| `ramstein calm <target>` | the gentle lever — `memory.high` nudge or renice; `--kill` escalates, confirmation-gated, per-invocation only |
| `ramstein advise` | nudges: leaks, runaway growth, a renderer swarm |

Modes: **Watch** (observe only — default) and an active mode that may apply
`calm` automatically and escalate to confirmation-gated kill — name **TBD**
(operator's ruling: not "steward"; nothing chosen yet). Kill itself is never
automatic in either mode — see Invariants.

## Invariants (hardcoded in the daemon; house security doctrine)

1. Nothing is killed without a **fresh, per-invocation confirmation** — no
   config flag, no persisting `--yes`, no non-interactive kill path.
2. `calm`'s levers are clamped: `memory.high` has a hardcoded floor computed
   from the target's own RSS, so it can never be set low enough to
   instant-thrash-OOM the thing it's meant to calm (phanspeed doctrine — a
   tampered config cannot weaken safety).
3. Socket input is hostile by default: SO_PEERCRED-gated, every field
   type-checked and clamped, capped reads, rate-limited commands
   (phanspeed's hostile-input socket doctrine, inherited wholesale).
4. A hardcoded exclusion set — PID 1, kernel threads, ramsteind itself — is
   never a valid target. Config can narrow the pool further, never widen it
   past this set (byebyte's category-detector doctrine, mirrored).
5. `status`/`oom`/`top` must answer from a bare TTY at 100% memory pressure —
   the daemon must not become the thing that gets OOM-killed while telling
   you about the OOM. Proven in CI with a cgroup `memory.max` squeeze.
6. Coexistence, not competition: if systemd-oomd or earlyoom is active,
   RAMstein's kill prompts stay advisory-only — it never races them.

## Milestones

- **M0 — truth engine.** `/proc/meminfo` + `/proc/pressure/memory` poller
  (~2s tick), EWMA burn rate, PSI-aware ETA-to-OOM, status.json, control
  socket. `ramstein status` e2e. Inherits byebyte's proven daemon/socket/
  status.json skeleton wholesale — same poll-loop shape, same `/run` pathing
  and perms — rather than reinventing it; only the memory math is new.
- **M1 — pill.** Collapsed: available memory + ETA-to-OOM, heats as ETA
  shrinks, red on a PSI `full` spike or an observed OOM kill. Expanded: PSI
  breakdown, top RSS growers. `make smoke` asserts status.json shape.
- **M2 — index.** Per-process + per-cgroup snapshot ring (sqlite, byebyte-
  style). `blame`, `swap`, `zombies`, `oom` ranking go live.
- **M3 — hands.** `calm` (memory.high / renice), `advise`, confirmation-gated
  kill escalation.
- **M4 — luxuries.** The active mode (name TBD), `smaps_rollup` PSS accuracy
  on demand, `.deb`, man pages.

## Stolen ideas ledger

byebyte: daemon/socket/status.json skeleton, sqlite snapshot-ring, "headroom
is effective headroom" rigor · phanspeed: missions (informing the unnamed
active mode), failsafe invariants, hostile-input socket doctrine ·
coldspot: ledger/advise tone, per-entity attribution · earlyoom/systemd-oomd:
proof PSI-driven kill timing beats free-memory thresholds — prior art for
`oom`'s ranking, not a dependency (stdlib-only; same kernel files, no lib).
