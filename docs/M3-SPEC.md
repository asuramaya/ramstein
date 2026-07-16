# RAMstein M3 — the hands (spec for till)

Authored by alfred, 2026-07-15. Doctrine: ~/code/REPOS/FAMILY.md; invariants:
PLAN.md (per-invocation-confirmed kill; systemd-oomd/earlyoom coexistence).
Version 0.4.0. stdlib only. Commit per green milestone on main.

## Scope

Three verbs land: `calm` · `oom` · `advise`. Nothing in M3 kills or throttles
without the invariant gates below — build the gates first.

## Invariant gates (first commit)

1. **Coexistence check** (PLAN Invariant #6): a helper that detects
   systemd-oomd (`systemctl is-active systemd-oomd`, read-only) and earlyoom.
   Every ACTION verb (calm's setters, kill) calls it and prepends a warning
   to its output when another OOM-fighter is live; `advise` reports it.
2. **Kill gate**: `calm --kill` requires an interactive TTY confirmation
   typed fresh per invocation (`really kill <pid> <comm>? type its PID to
   confirm:`). No `--yes`, no env override, no non-interactive path — a
   non-TTY stdin refuses outright. Socket layer: the daemon accepts
   `{"cmd":"kill","pid":N,"starttime":S,"confirm":N}` only with matching
   pid+starttime (stale-pid protection) — the CLI is what gates; the daemon
   validates identity. Never signal pid 1, kernel threads, or the daemon.

## Verbs

- `calm <pid|comm> [--high SIZE|--release|--nice N|--kill]` — the gentle
  lever first: resolve target (comm matches → require unique or refuse with
  the candidate list), find its cgroup from /proc/<pid>/cgroup, write
  `memory.high` (root daemon does the write via socket cmd; SIZE parsed
  like 500M/2G, clamped to [64M, MemTotal]). `--release` clears to max.
  `--nice` renice fallback when the cgroup path is unwritable. `--kill` as
  gated above, SIGTERM then optional SIGKILL prompt after 5s.
  Ledger every action to /var/lib/ramstein/ledger.jsonl (ts, verb, pid,
  comm, arg, result) — the family ledger, memory dialect.
- `oom` — the risk snapshot: MemAvailable+SwapFree, PSI trio, burn, ETA,
  then top-5 kill candidates by /proc/<pid>/oom_score with rss — "who dies
  first if this goes bad." Read-only.
- `advise` — rules, one line each + --json: (1) sustained PSI some_avg60 >
  threshold; (2) top RSS grower over the recent ring > X/hour; (3) swap
  occupancy > 50% names the top tenant; (4) zombie count > 0 names the
  negligent parent; (5) another OOM-fighter active → say which and stand
  down language; (6) eta_oom under an hour → point at `oom`.

## Smoke (fixture processes only)

- calm: spawn a child, `calm <pid> --nice 10` succeeds unprivileged; the
  memory.high path exercised against a fake cgroup dir via env override
  honored only when non-root (document the gate in a comment).
- kill gate: pipe a wrong confirmation → refused; non-TTY stdin → refused;
  correct TTY flow is untestable in CI — assert the refusal paths and the
  daemon's pid+starttime mismatch rejection ({"cmd":"kill"} with stale
  starttime → error).
- oom: shape assert (candidates sorted by oom_score, all fields present).
- advise: zombie fixture → rule 4 fires; assert coexistence line when a
  fake `systemctl` shim on PATH reports oomd active.
- Hostile: kill with string pid, calm with unknown comm, high with "-5G" —
  all errors, daemon alive.

## Gate

make smoke green; py_compile; healthcheck 0; CHANGELOG `## 0.4.0 — M3 the
hands`; VERSION + daemon constant. Mail alfred the smoke tail + one ledger
line from the calm test. M4 GO follows review.
