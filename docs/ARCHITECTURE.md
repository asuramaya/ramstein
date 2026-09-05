# Architecture

ramstein is a daemon that owns the truth about live memory (`ramsteind`), a verb CLI over it
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
ramstein owns bytes alive: RSS, swap contents, PSI, PIDs, who is holding memory hostage right
now. Two edge cases are deliberately not deduplicated between the two tools:

- tmpfs is memory-backed, so both tools see it. byebyte reports it df-shaped (bytes written,
  quota left); ramstein reports it memory-shaped (paged like anonymous memory, competing for
  the same PSI). Same bytes, two different questions: "why is /tmp full" and "why is memory
  tight" want different tools.
- Swap splits down the middle. The swap file's disk footprint (size, growth, whether it is
  about to fill the partition) is byebyte's. Swap occupancy, who is actually parked in it via
  `VmSwap`, is ramstein's. byebyte can say the swapfile is 98% full; only ramstein says who is
  in it.

## Data sources

`/proc/meminfo` (`MemAvailable`/`SwapFree`, cheap, read every tick) and `/proc/pressure/memory`
(PSI some/full at avg10/60/300, kernel-computed, needs `CONFIG_PSI`, on by default for years)
feed the burn-rate EWMA and the ETA-to-OOM. `eta_oom_seconds` is a straight division (headroom /
the EWMA'd rate), which amplifies any residual wobble in that rate nonlinearly — live-observed
swinging 61m → 69m within 30 seconds under a churning workload (alfred's finding, msg 6386,
2026-09-02). The daemon's own JSON keeps the raw number; the CLI (`human_oom_eta` in `src/bin/
ramstein`) and the daemon's own advise rule 6 (`_eta_bucket_text`) both coarsen it to a bucket
between 2 minutes and 2 hours (`<5m`/`<15m`/`<30m`/`<1h`/`<2h`) rather than show an exact minute
count the estimator can't back — the same discipline as the shmem split below, an honest number
kept from wearing an overconfident sentence. `/proc/<pid>/status` (`VmRSS`/`VmSwap`/`VmHWM`) is
the workhorse for `top`/`blame`/`swap`, one read per pid; summed `VmSwap` will not exactly equal
`SwapTotal - SwapFree`, since shared/CoW pages count against each holder (the swap analogue of
byebyte's df-vs-du honesty). `/proc/<pid>/smaps_rollup` gives an accurate PSS with shared pages
divided fairly, an order of magnitude more expensive than `status`, so it is sampled on demand
rather than every tick. cgroup v2 `memory.current`/`memory.pressure` gives per-cgroup PSI and is
the write target for `calm`. `oom_score`/`oom_score_adj` ground `oom`'s ranking in the kernel's
own math rather than reinventing it.

The `advise` shmem rule (rule 7) points a reader at byebyte for `/proc/meminfo`'s `Shmem`, but
byebyte is a path-indexing tool and roughly half of a real machine's Shmem is memfd/anonymous
shared segments with no filesystem path at all (chrome's and postgres's own shared mappings,
live-measured 2026-09-02: 4.17G tmpfs-reachable of 9.22G total, alfred's msg 6386) — a referral
covering under half a number while reading as though it covers all of it. `_tmpfs_reachable_bytes()`
sums every mounted tmpfs's used bytes via `statvfs` (the exact ceiling byebyte can ever itemise);
the rule then names both halves explicitly rather than handing the whole number to a tool that
can only account for part of it, falling back to the old undifferentiated wording when the split
can't be measured or disagrees with `Shmem`'s own snapshot (two separate kernel reads a moment
apart, under load, can disagree) rather than fabricate one.

`kills` reads a different source entirely: the kernel ring buffer, via `journalctl -k` (on-demand
subprocess call, not sampled or persisted — a kill is a rare, bursty, already-logged event, not
worth a poll-loop cost). Every other verb above answers "what's alive right now"; `kills` exists
because that question is structurally blind to a process the kernel already killed — there is no
`/proc/<pid>` left to read. Motivated by a live field incident (2026-08-18): a repo's e2e test was
OOM-killing a headless-Chrome child repeatedly (hard `memory.max` inside its own transient systemd
scope), diagnosed entirely by hand (`journalctl -k | grep -i oom`) because no verb here had
anything to say about it. Parses the three-line kernel burst a human would have grepped (`<comm>
invoked oom-killer`, the `oom-kill:constraint=...,oom_memcg=...,pid=...` accounting line, `Memory
cgroup out of memory: Killed process ...`), correlating the middle line's cgroup path onto the
kill line's pid — the one piece of context `top`/`blame` can never carry, since they're per-process
RSS with no cgroup/scope attribution at all. Diagnostic only, deliberately not wired into `oomd`
enrollment: a hard memcg kill is enforced by the kernel inside a cgroup ramstein doesn't own or
monitor, orthogonal to `systemd-oomd`'s own swap/pressure-triggered kills (see "The watchman and
auto-calm" below) — this verb explains a kill after the fact, it cannot prevent one.

## The index

The per-process sampler walks `/proc/[0-9]*/status` and `stat` every `sample_every` poll ticks
(default 3, so 30s at the 10s default poll interval) and writes into a ring-buffered sqlite
index at `/var/lib/ramstein/index.db` (`RAMSTEIN_STATE_DIR` env override). The identity key is
`(pid, starttime)`, not bare pid, so a reused pid is never mistaken for the process that held it
before. Two rings share one `promoted` flag column: `recent_ring` keeps every sample (about an
hour at defaults), `hourly_ring` keeps one promoted sample per hour (about a week). Schema stays
flat (`samples(id, ts, promoted, below_floor)`, `proc_stats(sample_id, pid, starttime, comm, rss,
swap, state, ppid)`, `WITHOUT ROWID`), WAL mode, one writer thread, short-lived read connections
for queries, mirroring byebyte's own sqlite discipline. Only processes clearing `proc_min_bytes`
(default 16 MiB) earn a row; a full pass over `/proc` has to stay comfortably under CI's timing
canary even on a busy box, since the sampler runs inline with the poll loop.

### `proc_min_bytes`: what the floor is for, and what happens under it

Design pass, thread 3dd73060 (alfred's dispatch, DM 4549) — a written finding, not a changed
constant, per his own instruction not to move the number just to have moved something.

**What it protects against.** Purely a *daemon cost* floor, not a relevance judgment: `_sample()`
runs inline with the poll loop, so the number of `INSERT`s per tick has to stay small enough that
a full `/proc` walk never threatens CI's own timing canary on a busy box (confirmed against this
file's own paragraph above, predating this pass). It was never a claim that a 10 MiB process is
memory that doesn't matter.

**What happens to what falls under it.** Every process under the floor is silently dropped —
`_sample()` filters them out of `all_procs` before a single row is written, so nothing about a
sub-floor process survives: not the process itself, not a count, not an "other" bucket. `top`,
`blame`, and `swap` all read this same index, and before this pass none of them said so: the CLI's
own man page never mentioned the floor at all (only `ramsteind(8)`'s config-reference table did,
which a `ramstein top` user has no reason to open), and the empty-list fallback text already said
"nothing above the index threshold" while the non-empty case — the one that matters, a handful of
big processes shown alongside many small ones genuinely invisible — said nothing. That is
practice 2c45d78e's trap exactly: the surface asserted "nothing here" when the honest claim was
"nothing above 16 MiB", and the two differ precisely when a machine is dying of a thousand small
things, which is the case this tool exists for. Measured live on the operator's own machine
(2026-08-15, default 16 MiB, real `/proc`): 272 of 278 running processes fell under the floor in a
single sample — not an edge case, the ordinary state of a normal desktop. Fixed by disclosure, not
a different number: `samples.below_floor` now records how many processes each sample excluded;
`top`/`swap`/`blame` surface it (JSON `below_floor` / `head_below_floor` + `base_below_floor` for
`blame`, plus a plain-text caveat line when nonzero), and `blame` additionally names its own
sharper caveat — a process crossing the floor between the two samples being diffed reads as fully
new or fully gone rather than partially grown or shrunk, a boundary artifact this pass names
rather than solves (solving it costs the same per-sample row count the floor exists to bound).

**Absolute, not relative — and that's where the floor's justification is weakest.** 16 MiB is a
fixed byte count, not a fraction of `MemTotal`; on this operator's 61 GiB machine it is roughly
0.025% of RAM, essentially costless to accuracy. On a small or memory-constrained box it is a much
larger bite, and that is exactly the box where the *daemon-cost* argument for having a floor at
all is weakest — fewer total processes exist to walk, so the `/proc`-walk-speed risk the floor
protects against barely exists there. The constant is not wrong on the machines this project has
actually been developed and measured against; it is a live tradeoff on the smaller ones this `.deb`
could ship onto, undocumented until this pass and still not solved by it — worth a future
`sample_every`-style config knob (`proc_min_pct` of `MemTotal`, floor'd at some absolute minimum)
if a small-box report ever surfaces one, but not built speculatively here.

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

If systemd-oomd or earlyoom is already active, every action verb and `advise` say so. ramstein
never races another OOM-fighter; its kill prompts stay advisory information rather than an
automatic stand-down, since that decision is still the human's and still requires the same TTY
confirmation.

Every `calm` or `kill` action is ledgered to `/var/lib/ramstein/ledger.jsonl` (timestamp, verb,
pid, comm, argument, result), the family's ledger pattern in ramstein's own dialect.

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
autocalm arm`/`dry`, config-persisted as `auto_calm_armed` so it survives a daemon restart once
armed — this was runtime-only, always resetting to disarmed, through v0.11.1; that shape modeled
itself on the kill gate's fresh-confirmation-every-time discipline, which is right for kill
(irreversible) and was wrong here, since renice and a `memory.high` squeeze are both reversible.
The TTY confirmation on the first `arm` is unchanged and is the actual consent gate; only the
amnesia on restart was the defect, fixed per DM #3074/#3079/#3080), and the
`ramstein-autocalm.timer` unit being manually enabled. The response is graduated and stops well
short of killing: renice the current top RSS grower, then, if that is not enough, a cgroup
`memory.high` squeeze to a configured percentage of the target's current RSS, always at least
110%, since `calm --high`'s own floor never allows less and a lower request would silently be a
no-op. There is no step past squeeze. The daemon never kills anything on its own at any setting;
a triggered cycle only ever surfaces the one `calm --kill` command it will not run itself, as a
desktop notification.

That notification goes through the pill for a design reason, not a technical one: `ramsteind`
only ever writes the suggested action into `status.json`'s `autocalm` field, and it is the pill,
running in the user's own session, that turns that into a real `Main.notify()` call. This was
believed to be a hard constraint ("a root daemon cannot reach a user's desktop session") until
V3 (below) disproved it directly — the daemon computes here because autocalm's suggestion is
tied to the kill gate's own machinery and belongs next to it, not because the daemon has no other
way to speak.

## V3: the daemon's own unprompted voice

Alfred's ruling (msg 6500, 2026-09-02), after the operator's verdict that ramstein and byebyte
both "feel kind of useless": every finding above is available only to someone who types a
command. `grep -c notify_owner src/bin/ramsteind` returned 0 before this section existed — zero
paths by which ramstein could tell its owner anything, unprompted, ever.

The mechanism: `ramsteind` connects straight to the owner's own D-Bus session bus and calls
`notify-send`, going around the GNOME pill entirely — `sudo -u '#<owner_uid>' env
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus notify-send ...`, best-effort, a warning
to stderr (journald, under the real unit) on failure, never blocking the poll loop. This directly
falsifies the "a root daemon cannot reach a user's desktop session" belief the autocalm
notification above was built around — verified live, not assumed: a real notification was sent
and the operator confirmed it rendered on the actual desktop before this was called done (alfred's
explicit condition; a notification path that is reachable but silent would be the exact defect
class this family spent the whole preceding month eliminating, shipped inside the tool built to
stop it). Right for the current machine's own state, too: its GNOME Shell has not reloaded in
three days, so every pill's extension code is staged-but-never-executed — a notification that
went through the pill would be provably invisible here regardless of how correct its logic was.

One category ships first: a post-kill notice, built on `kills` (fa65eb2). It is a kernel FACT
about the user's own machine after the event, not a forecast that can be wrong, which is why it
ships ON by default while later, forecast-based categories (an ETA crossing, a named runaway
process) ship OFF until this one has been trusted for a while — a wrong forecast poisons trust in
everything ramstein says afterward, but a kill notice has no false-positive surface at all: it
either happened or it didn't. The high-water mark (last-reported kill timestamp) persists to
`STATE_DIR/kill_notify.json` rather than staying in-memory, specifically so a daemon restart does
not re-report a kill from hours ago; a fresh install bootstraps the mark to "now" on its first
tick rather than scanning back through the machine's whole kill history. A burst of kills (this
session's own field incident: 5 in under 30 minutes) collapses to one notification naming the
most recent, not one popup per kill — five back-to-back interruptions for one underlying incident
would spend the trust this feature exists to build, not build it.

Deliberately SPEAKING, NOT ACTING: this reads `query_kills` and sends a best-effort notification;
it writes nothing any other verb reads back, and touches neither the kill gate nor autocalm's
three gates. Per-category config keys (`notify_kill_enabled` today), never a single global
switch — a user who trusts the kill notice and distrusts a later forecast-based category must not
have to choose one row. The CLI and `status.json` remain the full record regardless of whether a
notification fires or is silenced; a notification is advisory exhaust from computation the poll
loop already does, never a second source of truth.

### `standing`: stock, not flow

Alfred's ruling, msg 6644, after the operator said "keep digging" past the notification gap:
every verb above measures a RATE — burn, PSI, ETA, swap velocity — and a rate-watcher is
structurally blind to a pile that has stopped growing. Measured live: this machine had zero real
OOM events across five boots, swap never engaged, yet 3.4G of `/tmp` was held by 226 abandoned
pytest-xdist directories from finished test runs — dead weight that never crosses a threshold and
therefore can never trip an alarm. `standing` answers "what is holding memory that is already
dead" instead of "is memory about to run out," on demand, never on a timer (an accounting is not
a monitor).

Three tiers, deliberately rendered in three different confidence registers so a reader can tell a
sampled fact from a new attribution from a disclosed-but-unverdicted number:

- **stock** — `blame`'s exact inverse over the same sampled index, sign flipped: a process
  counts only if it clears `standing_min_bytes`, is present in BOTH the base and head samples
  (a genuinely new process can't have been flat for a day — that's `blame`'s own new/gone case),
  and hasn't moved more than `standing_flat_pct` across at least `standing_window_hours`. Costs no
  new instrumentation — "what grew" and "what's just sitting there" are the same index read.
- **anonymous/memfd** — the half of the shmem split (rule 7, above) byebyte can never duplicate,
  path or no path: which PID is actually holding the pathless-shared bytes `_tmpfs_reachable_bytes`
  already proved aren't in any real mount. `smaps_rollup`'s own `Shared_Clean`/`Shared_Dirty`
  fields (used elsewhere for PSS) can't do this job — they're an aggregate across every mapping,
  memfd and ordinary shared libraries alike, with no way to tell them apart — so this walks each
  process's full `/proc/<pid>/smaps` on demand instead, filtering VMAs by path shape verified live
  on the operator's own machine (2026-09-03): chrome's `/dev/shm/.com.google.Chrome.*` segments
  (even `(deleted)`, since unlinking a tmpfs file doesn't free its tmpfs blocks) are tmpfs-backed
  and excluded; pulseaudio/wayland's `/memfd:*` segments have no backing mount at all and are
  exactly the unattributed remainder. This is the strongest claim to necessity in the whole
  feature — byebyte has no visibility into anonymous memory regardless of path.
- **swap watermark** — the peak fraction of swap ever used, persisted to
  `STATE_DIR/swap_watermark.json` (updated once per poll tick, an in-memory compare, a disk write
  only when the peak rises), reported as a disclosed fact with NO recommendation attached. "Swap
  has never moved" is worth knowing; it is not evidence the configured size is wrong — absence of
  use says nothing about whether a future burst will need it (this session's own hector-vector
  incident hit an 8G cgroup ceiling, precisely the event swap exists to catch), and whether the
  disk space is worth reclaiming is byebyte's valuation, not ramstein's. Forward-looking only, not
  a retroactive reconstruction of boot-spanning history: day one can only say "since tracking
  began."

A fourth, related half was explicitly DECLINED: naming which files sit in `/tmp` and how old they
are (the 226 pytest-xdist directories themselves) is a filesystem question that happens to be
about a tmpfs — exactly the "Boundary, versus byebyte" doctrine above, and building it here would
have quietly duplicated byebyte's own index in a second daemon that would then have to be kept
correct forever. `standing`'s own tmpfs figure names the aggregate byebyte can, in principle,
itemise, and stops there — never promising coverage on the other side that hasn't been verified.

### `incidents`: fire marshal, not firefighter

Alfred's ruling, msg 7068/7077, 2026-09-05: `standing` and the swap watermark can only ever SAY a
peak happened; neither could answer "who was resident when it did" after the fact. Live case: a
20.2G/40G (50.5%) swap peak, real and unexplained — the watermark reported it as a bare number,
and there was no way to reconstruct what was actually holding memory at that moment.

`incidents` snapshots the top `incident_max_residents` processes by rss+swap the instant a real
threshold crosses: `incident_swap_pct` (a plateau/stock trigger, deliberately independent of
`swap_storm`'s own rate+ETA gate above — a slow sustained climb to 50%+ can cross this without
ever tripping a swap-storm warning, which is exactly the shape the operator's own 20.2G case took),
the existing `hot_psi_full` threshold (reused, not duplicated), or `swap_storm` itself becoming
active (a real V2.M1 event marker). Each trigger fires once per rising edge, not repeatedly while
the condition holds — the same hysteresis discipline `swap_storm` already uses for its own
clear-side debounce, applied here to the entry side instead, since a repeat record for a condition
that never cleared would just be noise stacked on the first, honest one. Multiple triggers on the
same tick collapse into ONE record naming all of them — they're one moment, not several.

The snapshot itself is deliberately cheap: residents come straight off the M2 sampler's own most
recent sample (no fresh `/proc` scan, no `smaps` walk) — alfred's explicit instruction was that a
prompt trigger with a cheap sample beats a slow one with attribution, and a human can run
`standing` by hand for the expensive per-process detail once they know an incident happened.
Persisted to `STATE_DIR/incidents.jsonl`, append-only, same shape as the existing `ledger.jsonl`
(calm/kill actions) — one line per incident, trimmed to `incident_max_records` on write (oldest
dropped first, no time-based expiry, since incidents are rare by construction: a real threshold
had to cross for one to exist at all). Each rendered line names its own basis in the sentence
itself ("swap crossed 50% (20.2G of 40G)") — the same "an inference must carry its basis in the
same sentence" discipline the swap watermark and shmem split already established, so a reader
never has to take the record's existence on faith. Report-only, same as `standing`: this reads
`poll_memory`'s own computed values and the sampler's existing index, writes nothing any other
verb reads back, and touches neither the kill gate nor autocalm's three gates.

## V4: the memory stance

Operator ruling, relayed msg 7110/7117/7119/7123/7127, 2026-09-05: ramstein stops being a pure
gauge on a system that already has one. The kernel and `systemd-oomd` manage pages by PRESSURE,
blind to INTENT, and systemd's own per-cgroup `memory.low`/`memory.high`/`swap.max`/OOM-preference
knobs sit at their defaults on every desktop. The stance is a short policy in the operator's own
words — PROTECT osiris-pg and the agent fleet, EXPENDABLE Chrome, CAP dev containers — kept applied
as cgroup memory controls while random-named scopes appear and vanish. A ceiling **throttles and
never kills** (measured directly: a disposable hog capped at `memory.high` pinned at its ceiling
with continuous reclaim events and zero OOM kills for the whole run); kill stays a human TTY verb,
untouched by any of this — see "The hands" above.

### The stance file

`/etc/ramstein/stance.json`. **Ships with zero rules** — the default stance protects nothing and
caps nothing until the operator names a rule. This file is never written by the daemon itself:
`ramstein ledger` runs first so the operator sees the machine's real shape, then hand-edits/enables
a seed rule set.

```json
{"rules": [
  {"match": {"comm": "postgres"}, "tier": "protect"},
  {"match": {"exe_glob": "~/.local/share/claude/versions/*"}, "tier": "protect"},
  {"match": {"unit_glob": "app-*.google.Chrome-*.scope"}, "tier": "expendable"},
  {"match": {"container_glob": "docker-*.scope"}, "tier": "cap", "memory_high_pct_of_total": 25}
]}
```

Rules match **leaf scopes only** (an earlier draft's `user@*.service` glob matched a *parent* of
every app scope, Chrome included — corrected before this shipped). Match keys: `unit_glob` (the
cgroup's own basename), `comm`, `exe_glob` (`/proc/<pid>/exe`, needed for anything whose `comm` is
not a stable name — the fleet's own `comm` is a version string), `container_glob`. First
match wins; nothing that matches gets `"tier": "unclassified"` — reported honestly by `ledger`,
never capped. Inventing a policy for a process nobody named is exactly the "rogue process" case
the operator wants surfaced, not silently squeezed.

A stance file that fails to load or validate (bad JSON, an unknown tier, a `cap` rule with no
size) **applies nothing** — `apply_stance` rolls back every previously-touched cgroup and reports
the error, the same as a daemon stop. Partial application of a broken file was never an option
considered.

### The three tiers

**protect** — measured directly (a synthetic three-level nested cgroup test, isolated from the
real machine): a leaf's own `memory.low` does **nothing** against reclaim pressure originating
above an ancestor whose own `low` is unset. Two leaves racing for a shared parent's budget split
cleanly along their own `low` values; the same leaves one hop deeper, behind a parent with no `low`
of its own, converge to *nearly equal* usage regardless — the leaf's reservation buys it nothing.
Real protection needs `memory.low` written on the target **and** every shared ancestor up through
`user@<uid>.service` and `user-<uid>.slice`, sized to the sum of every protect-tier leaf's own
**resident** bytes — not `memory.current`, which also charges file cache — and capped at
`stance_protect_floor_ceiling_pct` (default 50%) of `MemTotal` — a leak inside a protected scope
must not become an unbounded reservation; the ledger names it "pinned at ceiling" when this bound,
not real usage, is what's currently applied. Floor-on-resident is a deliberate item-5 ruling
(alfred msg 7436): `memory.low` exists to keep the fleet's own working state warm, not to pin file
cache the kernel could re-read in seconds — cache above the resident floor inside a protected
scope stays reclaimable under pressure, and that's a performance cost only, the correct one. Every
applied floor carries its own basis string (`"resident of 20 processes in 1 scope"`) in `stance status`/`plan`
output, so the number is never reported bare. The **cap** tier's ceiling, by contrast, stays sized
against `memory.current` (charged, cache included): it is the worst-case backstop, and a backstop
must bound everything the kernel is holding, not only the anonymous working set.
`ManagedOOMPreference=avoid` rides
along on the leaf itself. Deliberately stops at `user-<uid>.slice` — never touches the top-level
`user.slice` (shared by every user on the machine) or `system.slice` (every root service): a leaf
living under `system.slice`/`docker-*.scope` (osiris-pg) gets leaf-level protection only, a
documented, accepted limit rather than a per-app tool reaching into the whole machine's services.

**expendable** — no standing squeeze. An earlier draft kept `memory.high` permanently close to
Chrome's own usage regardless of system state, which is a tax paid whether or not anyone needs the
memory — dropped before shipping. Instead, expendable's `memory.high` rides `do_autocalm_run`'s own
three consent gates (`auto_calm_enabled`, armed, the `ramstein-autocalm.timer` unit) and its
existing squeeze mechanism as one more step (`stance_squeeze`), sized to
`stance_expendable_squeeze_pct` (default 70%) of the leaf's own current usage, applied only while
`_autocalm_trigger` reports the system hot and released — back to `max` — the instant it calms.
Unlike the pre-existing top-RSS squeeze (never self-releasing, an operator's own `calm --release`
job today), this one lifts on its own. No protection is applied; `ManagedOOMPreference` is left at
its default so `systemd-oomd`'s own selection naturally reaches it first.

**cap** — a standing `memory.high`, fixed (`memory_high_bytes`) or relative
(`memory_high_pct_of_total`). Containers are capped by design intent, not by system state, so this
tier needs no hot/calm gating — applied on the daemon's own regular poll cycle, same as protect.

### The road: bridge, or plain root

A cgroup living inside a `user@<uid>.service`'s own delegated subtree is written via the exact
`sudo -u '#<uid>' env DBUS_SESSION_BUS_ADDRESS=... systemctl --user set-property` bridge V3's
desktop notification already uses — measured directly: a raw root write straight into cgroupfs
under a delegated subtree is the wrong road (this session's own sandbox refused even the attempt);
the D-Bus bridge is the one that sticks, atomically, without racing the owning user manager's own
bookkeeping. Everything else — `system.slice`, `docker-*.scope`, or the two session-level units
*above* the delegation boundary (`user@<uid>.service` and `user-<uid>.slice` themselves) — is
root's own cgroup outright: a plain `systemctl set-property`, no bridge. `ledger` shows which road
each managed scope took.

### `ramstein ledger`

Memory **by thing, not by pid** — the operator's own ask: one line for thirteen Claude sessions,
not twenty-six rows. Same-tier instances collapse into one row (count, resident bytes, cgroup-
charged bytes, which road(s)); `unclassified` groups by `exe` (falling back to `comm`) so an
unnamed population still reads as one line instead of fragmenting the moment nothing in the stance
names it. Meaningful even against the shipped zero-rule stance — everything just reads
unclassified, grouped the same way.

### `ramstein stance rollback`

Walks every cgroup property the daemon has *ever* written (persisted to
`STATE_DIR/stance_touched.json`, survives a restart), resets each to its off value
(`memory.low`→0, `memory.high`→`max`, `ManagedOOMPreference`→`none`) via the same road it was
written with, reads it back to confirm, and reports per-entry pass/fail. A unit that no longer
exists counts as a pass — nothing left to reset. Runs automatically before the stance is ever
(re-)applied on daemon start, and is the daemon's own response to a stance file that fails to
load — a clean slate before either reapplying or refusing.

### `ramstein stance plan`

The operator must be able to read what a stance would do before the first real application —
the same road byebyte's Storage Sense took with `dry_run`. `plan` runs the *exact same*
`_apply_protect_tier`/`_apply_cap_tier` computation the real apply uses, just with `dry_run=True`
— one code path for both, so a plan can never quietly drift from what applying would actually do
— and never calls `_systemctl_set_property`, never touches `stance_touched.json`. `--file PATH`
previews an explicit file (the shipped `stance.example.json`, or a draft in progress) instead of
the configured `stance.json`; a missing `--file` is reported as an error, not silently read as
zero rules, since asking to preview a file that isn't there is a mistake worth naming.

### incidents cites the stance

`incidents`' own snapshot (above) now names which tier pushed back first when a real threshold
crossed — "Chrome pushed out first, per your stance" — by cross-referencing the snapshot's top
residents against the stance classifier at the moment the trigger fired. This classification pass
is deliberately **not** run on every poll tick (that would defeat incidents' own cheap-trigger
design) — only on the rare tick a trigger actually fires.

## The sutra backbone

`src/share/ramstein/lib/sutra.py`, `sutra_update.py`, and `sutra_xen.py` (plus
`src/extension/ramstein@asuramaya/pill.js`) are vendored byte-identical from the family's shared
`sutra` commons, never hand-edited; a re-vendor is the only way they change. `make check-sutra`
is the drift guard: integrity (the file's sha256 against its own `.version` anchor) is a hard
failure on any mismatch, and freshness (only checked when a canonical `sutra` checkout is
present) reads the `.commit` anchor and asks canonical git whether it is an exact match, a lag
(an ancestor of current HEAD, a stale but honest vendor, warns only), or drift (not an ancestor
at all, a corrupted anchor or a rewritten canonical history, hard fails).

ramstein was the family's pilot (alfred, DM #2716) for vendoring the *recipe* the same way as the
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
`ewma_rate` wants the quantity whose *increase* is the burn, so ramstein passes `total - avail`
(the used-equivalent) rather than avail itself. `ramstein` (the CLI) gets its socket client and
status fallback from `sutra.request`/`sutra.read_status`. `ramstein-update` is a thin wrapper
over `sutra_update.main()`, the family's shared update spine with its three consent tiers and
SSH-signature verification. `sutra_xen.py` ships because the family vendors the full set even
where it is not yet imported (ramstein has no Xen guest-surface concerns wired in today);
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
and the pill as-is; ramstein trusts the kernel's own accounting more than it distrusts a
process's own chosen name, unlike a tool parsing attacker-controlled network input.

## Standard exemptions

No declared exemptions. ramstein already ships a daemon, man pages for both binaries, an attack
suite over the full command surface, and a release-signing anchor; nothing in
`REPO-STANDARD.md`'s required shape is missing here.
