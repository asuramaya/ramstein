# Architecture

RAMstein is a daemon that owns the truth about live memory (`ramsteind`), a verb CLI over it
(`ramstein`), and a GNOME Quick Settings pill on top. The CLI and the pill never read `/proc`
or a cgroup directly; they read `status.json` or send a command over the control socket, and
the daemon is the only thing that ever touches a privileged path.

## Repo map

```
src/bin/                ramsteind (daemon), ramstein (CLI), ramstein-healthcheck, ramstein-update
src/share/ramstein/lib/ vendored sutra.py / sutra_update.py / sutra_xen.py + their .version/.commit
                        anchors (BOOTSTRAP.md's private per-pill path, mirrors the installed layout)
src/data/config/        config.json defaults (seed, never master)
src/data/man/           ramstein.1 (man1), ramsteind.8 (man8)
src/data/systemd/system/ ramsteind.service, ramstein-update.timer/.service, ramstein-autocalm.timer
src/extension/          the GNOME pill (ramstein@asuramaya), vendored pill.js
packaging/              VERSION (the one version constant), packages.txt, deb/, release-signing/
tests/                  smoke.sh, attack_socket.py, zombie_maker.py
docs/                   this file, USAGE.md, RELEASING.md, RELEASE-SIGNING.md, CHANGELOG.md
```

`src/` answers "what is this thing", `packaging/` answers "how does it become a release",
`.github/` carries the community files and CI workflows. Installed paths never moved when this
tree was built: only the source layout changed (REPO-STANDARD.md's tree pass), so a `.deb` or an
`install.sh` run still lands binaries in `/usr/bin` or `/usr/local/bin` exactly as before.

## Boundary, versus byebyte

byebyte owns bytes at rest: every filesystem, including tmpfs, file usage and quota headroom.
RAMstein owns bytes alive: RSS, swap contents, PSI, PIDs, who is holding memory hostage right
now. Two edge cases are deliberately not deduplicated between the two tools:

- tmpfs is memory-backed, so both tools see it. byebyte reports it df-shaped (bytes written,
  quota left); RAMstein reports it memory-shaped (paged like anonymous memory, competing for
  the same PSI). Same bytes, two different questions: "why is /tmp full" and "why is memory
  tight" want different tools.
- Swap splits down the middle. The swap file's disk footprint (size, growth, whether it is
  about to fill the partition) is byebyte's. Swap occupancy, who is actually parked in it via
  `VmSwap`, is RAMstein's. byebyte can say the swapfile is 98% full; only RAMstein says who is
  in it.

## Data sources

`/proc/meminfo` (`MemAvailable`/`SwapFree`, cheap, read every tick) and `/proc/pressure/memory`
(PSI some/full at avg10/60/300, kernel-computed, needs `CONFIG_PSI`, on by default for years)
feed the burn-rate EWMA and the ETA-to-OOM. `/proc/<pid>/status` (`VmRSS`/`VmSwap`/`VmHWM`) is
the workhorse for `top`/`blame`/`swap`, one read per pid; summed `VmSwap` will not exactly equal
`SwapTotal - SwapFree`, since shared/CoW pages count against each holder (the swap analogue of
byebyte's df-vs-du honesty). `/proc/<pid>/smaps_rollup` gives an accurate PSS with shared pages
divided fairly, an order of magnitude more expensive than `status`, so it is sampled on demand
rather than every tick. cgroup v2 `memory.current`/`memory.pressure` gives per-cgroup PSI and is
the write target for `calm`. `oom_score`/`oom_score_adj` ground `oom`'s ranking in the kernel's
own math rather than reinventing it.

## The index

The per-process sampler walks `/proc/[0-9]*/status` and `stat` every `sample_every` poll ticks
(default 3, so 30s at the 10s default poll interval) and writes into a ring-buffered sqlite
index at `/var/lib/ramstein/index.db` (`RAMSTEIN_STATE_DIR` env override). The identity key is
`(pid, starttime)`, not bare pid, so a reused pid is never mistaken for the process that held it
before. Two rings share one `promoted` flag column: `recent_ring` keeps every sample (about an
hour at defaults), `hourly_ring` keeps one promoted sample per hour (about a week). Schema stays
flat (`samples(id, ts, promoted)`, `proc_stats(sample_id, pid, starttime, comm, rss, swap, state,
ppid)`, `WITHOUT ROWID`), WAL mode, one writer thread, short-lived read connections for queries,
mirroring byebyte's own sqlite discipline. Only processes clearing `proc_min_bytes` (default 16
MiB) earn a row; a full pass over `/proc` has to stay comfortably under CI's timing canary even
on a busy box, since the sampler runs inline with the poll loop.

`blame --since T` is a join of two samples: grown, new (absent from the base sample), or gone
(absent from the latest, shown as freed, negative). `zombies` deliberately does not read the
index: a zombie is a now-question, so it walks live `/proc` and names the parent that is not
reaping.

## The hands: calm, and the kill gate

`calm <target>` resolves a pid or an exact comm name (an ambiguous comm refuses with the
candidate list rather than guessing) and applies one lever: `--high SIZE` writes the target's
cgroup `memory.high`, clamped to `[64M, MemTotal]` and to at least 1.1x the target's own current
RSS, so a tampered or fat-fingered size can never be low enough to instant-thrash-OOM the process
it is meant to calm. `--release` clears it back to max. `--nice N` renices, 0 to 19; calm only
ever lowers priority. `--kill` is the one ungentle lever, and it is gated hardest: a fresh,
per-invocation confirmation typed at a real TTY, no `--yes`, no environment override, no
non-interactive path at all (a non-TTY stdin is refused before the request ever reaches the
socket). The daemon independently re-validates `(pid, starttime)` identity against live `/proc`
before signaling, so a stale or reused pid is refused even if the CLI layer were somehow
bypassed. PID 1, kernel threads, and the daemon's own pid are hardcoded exclusions; config can
narrow the target pool further, never widen it past this set.

If systemd-oomd or earlyoom is already active, every action verb and `advise` say so. RAMstein
never races another OOM-fighter; its kill prompts stay advisory information rather than an
automatic stand-down, since that decision is still the human's and still requires the same TTY
confirmation.

Every `calm` or `kill` action is ledgered to `/var/lib/ramstein/ledger.jsonl` (timestamp, verb,
pid, comm, argument, result), the family's ledger pattern in RAMstein's own dialect.

## The watchman and auto-calm (V2)

Two always-on, read-only analyses run in the poll loop. A second EWMA tracks swap consumption
specifically, independent of the main burn EWMA; when it is actively growing and the combined
ETA (avail + swap_free over burn) crosses a configured horizon, `status.json` gains a `warning`
object naming the top three swap growers, and it is sticky: a run of consecutive clear polls is
required before it drops, so a value hovering right at the horizon does not flap the pill on and
off. This catches a gap the general avail%/PSI/eta classifier can miss: `MemAvailable` is a
reclaimable-cache-aware kernel heuristic that can look fine for a while even as swap visibly
drains. The zombie advisory is actionable the same way: once one parent accumulates enough
unreaped children, `advise` names the actual culprit instead of just a count.

Auto-calm arms the existing `calm` machinery to act on its own, and it is deliberately built as
three independent gates that all have to be true before anything real happens: the config master
switch (`auto_calm_enabled`, off by default), the runtime armed state (toggled only by `ramstein
autocalm arm`/`dry`, and always reset to disarmed on daemon restart, the same discipline the kill
gate uses against a remembered confirmation surviving a process boundary), and the
`ramstein-autocalm.timer` unit being manually enabled. The response is graduated and stops well
short of killing: renice the current top RSS grower, then, if that is not enough, a cgroup
`memory.high` squeeze to a configured percentage of the target's current RSS, always at least
110%, since `calm --high`'s own floor never allows less and a lower request would silently be a
no-op. There is no step past squeeze. The daemon never kills anything on its own at any setting;
a triggered cycle only ever surfaces the one `calm --kill` command it will not run itself, as a
desktop notification.

That notification is architecturally interesting: a root daemon cannot reach a user's desktop
session, so `ramsteind` only ever writes the suggested action into `status.json`'s `autocalm`
field. It is the pill, running in the user's own session, that turns that into a real
`Main.notify()` call. The daemon computes; the pill speaks.

## The sutra backbone

`src/share/ramstein/lib/sutra.py`, `sutra_update.py`, and `sutra_xen.py` (plus
`src/extension/ramstein@asuramaya/pill.js`) are vendored byte-identical from the family's shared
`sutra` commons, never hand-edited; a re-vendor is the only way they change. `make check-sutra`
is the drift guard: integrity (the file's sha256 against its own `.version` anchor) is a hard
failure on any mismatch, and freshness (only checked when a canonical `sutra` checkout is
present) reads the `.commit` anchor and asks canonical git whether it is an exact match, a lag
(an ancestor of current HEAD, a stale but honest vendor, warns only), or drift (not an ancestor
at all, a corrupted anchor or a rewritten canonical history, hard fails).

RAMstein was the family's pilot (alfred, DM #2716) for vendoring the *recipe* the same way as the
code: `src/share/ramstein/lib/sutra.mk`, included from the root `Makefile` (`PILL := ramstein`),
supplies `check-sutra` itself, the canonical tracked-files row count (`check-repo` references
`SUTRA_ROOT_ROWS` rather than re-deriving it), and `check-vendored-path` (loads a binary as a real
module and asks Python what it actually imported, rather than checking that a file merely exists
at the path the bootstrap preamble's own arithmetic predicts — the latter is a layout check, not a
resolution check, and passes on the exact regression it's meant to catch). The pilot found four
gaps sutra.mk didn't cover; all four folded upstream at 0.11.0/0.11.1 (msg 2783) rather than
staying pill-side supplements — `SUTRA_EXT_DIR := src/extension/ramstein@asuramaya` opts
`check-sutra` into also checking `pill.js` (was a separate `check-pill-js` target here), and
`SUTRA_CHECK_BINS := ramsteind ramstein ramstein-healthcheck ramstein-update:sutra_update` is the
native form of what was a hand-rolled `check-vendored-path-all` (`ramstein-update` binds
`sutra_update`, not `sutra`, hence the `:module` suffix on that one entry). Re-vendoring folded
a real defect too: 0.11.0's first `SUTRA_EXT_DIR` fix tested the variable at the Make level but
read it back at the shell level (never exported), so it silently checked nothing while exiting 0 —
worth remembering next time a green run is trusted without reading what it actually printed.

The vendored copies live in their own private, package-owned directory rather than beside the
binaries (BOOTSTRAP.md, ruling `3e44bd95`). Every pill vendoring `sutra.py` under the same
filename into the same shared bin directory (`/usr/bin` via `.deb`, `/usr/local/bin` via
`install.sh`) made any two pills installed together collide: `dpkg` refuses the second package
outright, and a plain `install` here has no ownership tracking and would silently overwrite,
anchors included. It was measured, not theorised: two pills on the same real machine were
already running different canonical commits of `sutra.py` with no anchor in that shared
directory to catch it. Each binary that imports `sutra` or `sutra_update` carries a small,
canonical bootstrap preamble (sutra publishes the exact text; every pill pastes it verbatim,
never hand-derived) immediately before the import, computing
`dirname(dirname(realpath(__file__)))/share/ramstein/lib` at runtime so it finds the vendored
copy whether that's `/usr/local` from a dev checkout's `src/bin/`, `/usr` from a `.deb`, or any
other install prefix. `ramstein-healthcheck` additionally verifies the INSTALLED sutra copy
against its own installed `.version` anchor, not just the checked-out one `check-sutra` covers,
closing the exact blind spot the collision itself exploited.

`ramsteind` gets its config loading, status writing, EWMA math, and control socket from `sutra`
(`load_config`, `write_status`, `ewma_rate`, `ControlServer`); the one subtlety is that
`ewma_rate` wants the quantity whose *increase* is the burn, so RAMstein passes `total - avail`
(the used-equivalent) rather than avail itself. `ramstein` (the CLI) gets its socket client and
status fallback from `sutra.request`/`sutra.read_status`. `ramstein-update` is a thin wrapper
over `sutra_update.main()`, the family's shared update spine with its three consent tiers and
SSH-signature verification. `sutra_xen.py` ships because the family vendors the full set even
where it is not yet imported (RAMstein has no Xen guest-surface concerns wired in today);
shipping less than the full set is exactly the bug Wave B fixed for the other three files.

## Security model

The relevant attacker is an unprivileged local process abusing the root daemon; `ramsteind` has
no network attack surface at all, and the only component that ever reaches the internet is the
separate, unprivileged `ramstein-update`. The socket is AF_UNIX, newline-delimited JSON, bounded
reads, and every connection is checked against `SO_PEERCRED`: only root or the configured
`owner_uid` may issue commands, on top of the socket's own 0660 mode. Malformed input,
non-objects, and unknown commands are answered with an error and the connection ends; none of it
crashes the daemon. The full config clamp table, the exact capability set the hardened systemd
unit retains, and the complete list of invariants live in `src/data/man/man8/ramsteind.8`; this section exists
so a successor does not have to open the man page to know the shape.

## Conventions worth knowing before you edit

Config is the seed, never the master: every key is typed and clamped on load, unknown keys are
ignored, and a tampered config can tune numbers within their clamps but never grant a new ability
or weaken a hardcoded invariant like the kill gate or the memory.high floor. The version appears
exactly once, at `packaging/VERSION`; nothing else carries a literal version string. `sutra.py` and its
siblings are vendored byte-identical; `make check-sutra` proves it, and the fix for drift is
always a re-vendor, never a hand-edit. Names that come off `/proc` (comm strings) reach the CLI
and the pill as-is; RAMstein trusts the kernel's own accounting more than it distrusts a
process's own chosen name, unlike a tool parsing attacker-controlled network input.

## Standard exemptions

No declared exemptions. RAMstein already ships a daemon, man pages for both binaries, an attack
suite over the full command surface, and a release-signing anchor; nothing in
`REPO-STANDARD.md`'s required shape is missing here.
