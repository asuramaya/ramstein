# RAMstein M2 — the per-process index (spec for till)

Authored by alfred (coordinator, house bytebye), 2026-07-14. Doctrine:
`~/code/REPOS/FAMILY.md`; design: `PLAN.md`. ByeByte's M2
(`~/code/REPOS/ByeByte/bin/byebyted`, Indexer class) is the pattern — same
sqlite discipline, memory dialect. Version 0.3.0. stdlib only. No commits.

## Scope

Four verbs land: `top` · `blame` · `swap` · `zombies`. A process sampler
feeds a ring-buffered sqlite index at `/var/lib/ramstein/index.db`
(`RAMSTEIN_STATE_DIR` env override, mirroring `BYEBYTE_STATE_DIR`).

## Sampler

Every `sample_every` polls (config, default 3 → 30s at the 10s poll), walk
`/proc/[0-9]*/status` and `/proc/<pid>/stat`:
- keep: pid, comm (Name), VmRSS, VmSwap, state, ppid, starttime (stat field
  22 — pid reuse disambiguator; the identity key is (pid, starttime))
- skip kernel threads (VmRSS absent); unreadable pids (other users, daemon
  running unprivileged) are skipped silently — as root it sees all
- write one `samples` row + per-proc `proc_stats` rows for processes with
  VmRSS+VmSwap ≥ `proc_min_bytes` (config, default 16 MiB, clamped ≥ 1 MiB)

Two rings (config, clamped): `recent_ring` — every sample, keep 120
(~1h); `hourly_ring` — one sample promoted per hour, keep 168 (7d).
Promotion = flag column, pruning respects both rings. Schema mirrors
ByeByte: interned comm strings are unnecessary (comms are short) — keep the
schema flat: samples(id, ts, promoted), proc_stats(sample_id, pid,
starttime, comm, rss, swap, state, ppid), PK(sample_id, pid, starttime),
WITHOUT ROWID. WAL mode. Writer = sampler thread only; queries open
short-lived read connections (ByeByte's exact pattern).

## Verbs (socket cmd + CLI verb + --json, replacing PLANNED stubs)

- `top [--swap] [--limit N]` — latest sample sorted by rss (or swap desc).
  Human: `RSS      SWAP    PID   PROC` columns, state flag on zombies.
- `blame [--since T] [--limit N]` — nearest sample ≤ since (fall back to
  oldest, report the honest span — see ByeByte's blame fallback) vs latest.
  Delta per (pid, starttime): grown, `new` (absent in base), `gone`
  (absent in head — show as freed, negative). Sort by |delta| desc,
  growth first.
- `swap [--limit N]` — latest sample, VmSwap desc, only rows with swap > 0.
  This is the "who is parked in the 5.8G" answer.
- `zombies` — state == 'Z' in the LIVE /proc (not the index — zombies are
  now-questions), each with ppid and the parent's comm, so the user knows
  who forgot to reap.

Hostile-input rules identical to the family: typed validation, ValueError →
{"error"}, unknown keys ignored, limits clamped 1..1000.

## Smoke (extend tests/smoke.sh — fixture processes only)

- Boot daemon with poll_interval 1, sample_every 1, proc_min_bytes at the
  clamp floor.
- Spawn a child that allocates ~100 MiB and sleeps; wait one sample; `top`
  must rank it in the visible set with rss ≥ 90 MiB.
- Capture ts, let one more sample land, `blame --since <ts>` shows the
  child as grown or new.
- Zombie: double-fork a child that exits unreaped, assert `zombies` lists
  it with the right ppid, then reap and assert it clears.
- Hostile: `{"cmd":"blame","since":"tuesday"}` → error; `{"cmd":"top",
  "limit":0}` → error; ping alive after abuse.

## Verification gate

`make smoke` green, py_compile, healthcheck exits 0 against the temp
daemon, CHANGELOG `## 0.3.0 — M2 per-process index`, VERSION + daemon
constant to 0.3.0. Mind the sampler's cost: one full pass over /proc must
stay under ~50ms at ~400 procs (time it in smoke, assert < 500ms — CI
machines are slow; the assertion is a canary, not a benchmark). Reply to
alfred via OSIRIS mail with the smoke tail and one real `swap` line from
this machine (5.8G is parked in swap here — name the top tenant).
