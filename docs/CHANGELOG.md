# Changelog

## 0.11.1 — sutra.mk / pill-ci.yml adoption
Structural only, no daemon/CLI/pill behavior changed. RAMstein piloted the family's shared
recipe layer ahead of the other four pills, so they could copy this diff rather than a
description of it (alfred's order, DM #2716).

- Vendored `sutra.mk` (the Makefile fragment sutra now publishes alongside `sutra.py`/
  `sutra_update.py`/`sutra_xen.py`) and replaced the hand-written `check-sutra` target and
  `check-repo`'s row-count logic with it. Found and reported upstream: the original loop never
  covered `pill.js` (a gap for 3 of 5 pills), `check-vendored-path` validated only one binary
  per call (RAMstein has four), `pill-ci.yml` shellchecked nothing, `run-check-version`
  defaulted on with no pill actually using it, and `SUTRA_CHECK_ARGS` defaulting to `--help`
  made `make check` place a real (harmless, read-only) call against the live daemon socket on
  every run. All five folded upstream into sutra 0.11.0/0.11.1.
- Adopted `pill-ci.yml` (sutra's shared reusable CI workflow), pinned by commit SHA. Split
  RAMstein's CI into the shared job plus a thin `ramstein-specific` sibling for the one thing
  sutra.mk still can't do package-agnostically: looping the resolution check across all four
  binaries.
- Re-vendored to 0.11.1 (DM #2783) once the four findings above landed upstream, deleting the
  two hand-rolled pilot supplements (`check-pill-js`, `check-vendored-path-all` as a
  hand-written target) now that sutra.mk covers both natively via `SUTRA_EXT_DIR`/
  `SUTRA_CHECK_BINS`. Verified directly, not just via green exit code, that the pill.js
  integrity check upstream's fix depends on actually executes rather than silently
  no-op'ing — the exact defect class this family of fixes exists to catch.
- Caught and fixed, before either report cycle closed: the pilot's first commit never set
  `run-attack` in `pill-ci.yml`'s `with:` block (defaults to `false`), so CI's adversarial fuzz
  pass silently stopped running the moment the pilot landed — invisible in both a passing local
  `make attack` and a green job-level CI summary, found only by reading per-step status off the
  GitHub API directly.

Verified: `make check`, `make smoke`, `make attack` all green on the real repo; CI green on
the tagged commit, checked per-step, not by summary alone.

## 0.11.0 — the sutra install-path adoption
Behavior-preserving for status.json/the control socket, but a real fix for a real collision.
Every pill vendors `sutra.py` byte-identical, but every pill's installer used to drop that copy
into the same shared bin directory under the same filename (`/usr/bin` via `.deb`,
`/usr/local/bin` via `install.sh`), so any two pills installed together collided: `dpkg` refused
the second package outright, and `install.sh`'s plain `install` silently overwrote, anchors
included. Found while trying to complete v0.10.0's own install-over-installed verification
(`dpkg -i` refused to overwrite `/usr/bin/sutra.py`, already owned by phanspeed's package), and
confirmed worse than it looked from inside this repo alone: two pills on the operator's own
machine were already running different canonical `sutra.py` commits, undetectable, since the
shared directory carried no `.version`/`.commit` anchors at all (ruling `3e44bd95`).

- The vendored `sutra.py`/`sutra_update.py`/`sutra_xen.py` (+ anchors) move to a private,
  package-owned directory: `src/share/ramstein/lib/` in the source tree,
  `<prefix>/share/ramstein/lib/` once installed (`/usr/share/ramstein/lib/` via `.deb`,
  `$PREFIX/share/ramstein/lib/` via `install.sh`, off the same `$PREFIX` the binaries already
  use). Anchors travel with the code, always.
- Every binary that imports `sutra` or `sutra_update` (`ramsteind`, `ramstein`,
  `ramstein-healthcheck`, `ramstein-update`) carries the canonical bootstrap preamble sutra
  itself publishes (BOOTSTRAP.md), immediately before the import, instead of relying on being
  co-located with `sutra.py`. The preamble computes its own directory at runtime, so it works
  unmodified whether the binary is running from a dev checkout's `src/bin/`, `/usr/local/bin`
  via `install.sh`, or `/usr/bin` via `.deb`, no `$PREFIX` ever hardcoded or handed in.
- `ramstein-healthcheck` now also verifies the INSTALLED `sutra.py` against its own installed
  `.version` anchor, not just the checked-out copy `check-sutra` covers. `check-sutra` only ever
  proved the repo copy wasn't hand-edited; the machine runs the installed one, and that gap is
  exactly how the collision on the operator's own machine went undetected.
- `install.sh` (and `uninstall.sh`, which already covered it) clean up the old
  `$PREFIX/bin/sutra*.{py,version,commit}` files unconditionally: a `.deb` upgrade drops
  package-owned files automatically, but `install.sh`'s old copies were never owned by anything
  and would otherwise linger forever.
- `pill.js` is exempt, unchanged: it already installs per-pill under its own extension
  directory and was never part of the collision.

Verified in full isolation, never against the operator's live system: a systemd-in-Docker
attempt failed in this sandbox (nested privilege isn't available here), so verification used a
plain container with a stubbed `systemctl`, matching the same fixture pattern `tests/smoke.sh`
already uses for the coexistence check. Confirmed live: the daemon actually imports `sutra` from
the new location (both from a raw dev checkout and from a real `install.sh` install inside the
container), a simulated pre-adoption leftover (fake old-style files planted in the old shared
bin dir) gets cleaned up by the new `install.sh`, three consecutive install runs stay idempotent,
and `uninstall.sh` removes everything cleanly. `make check` + `make smoke` + `make attack` green
throughout on the real repo.

This is one pill's half of a family-wide fix (sutra 0.8.0 publishes the preamble; each pill
adopts at its own next touch, sequenced by alfred, obligation `20819d5a`). The cross-pill dpkg
collision itself isn't fully closed until every pill has made this same move.

## 0.10.0 — RS-STD-1: the family repo standard
Structural only, no daemon/CLI/pill behavior changed. Adopted the family's REPO-STANDARD.md
in three passes (alfred's order, mail #1581), landing the same twelve-row root kast and
coldspot already reached.

- **Pass 1 (docs).** Wrote docs/ARCHITECTURE.md, docs/USAGE.md, docs/RELEASING.md. PLAN.md
  and the four tracked *-SPEC.md files graduated into ARCHITECTURE.md (the durable design
  half) and moved out to the seat's own office (the order half, now dead prose); the
  untracked V2-SPEC.md moved the same way. Fixed docs/RELEASE-SIGNING.md's stale "unarmed"
  claim: the anchor has carried all 4 canonical keys since commit 89acdc2, and this release
  is the first to actually ship it. README split along the standard's R1 rule: the old
  post-install verb table and milestone-status paragraph are gone, replaced by a "why not
  just top or earlyoom" pitch a stranger can use before installing.
- **Pass 2 (truth).** Collapsed the one real duplicate version constant: ramsteind's own
  hardcoded VERSION literal now reads the same installed-path VERSION file ramstein-update
  already searches, closing the drift class that left the 0.6.1 pill footer a release
  behind. release.yml's tag check now proves the daemon actually resolves the tagged
  version (a live SourceFileLoader import) instead of grepping a literal that no longer
  exists. Dropped release.yml's --generate-notes fallback for a thin CHANGELOG extraction;
  a missing section now refuses the release outright.
- **Pass 3 (tree).** bin/, extension/, systemd/, config/ moved under src/ (systemd and
  config into src/data/); man/ into src/data/man/{man1,man8}/; release-signing/,
  sync-signers.sh, seed-owner-uid.py, packages.txt, VERSION under packaging/; CHANGELOG.md
  into docs/; the three community files into .github/. No installed path changed, verified
  via byte-identical deb contents and two live install.sh install-over-installed runs
  against a real daemon. Added make check and make check-repo, the family's structural
  gate, copied from coldspot. Added .gitattributes: RAMstein had none, so CI/dev files were
  shipping inside every release tarball; a real git archive now confirms they don't.

Found while trying to complete the deb-layout half of the install-over-installed test:
every pill vendors sutra.py into the same shared bin directory under the same name, so any
two pills installed together collide (a .deb install refuses outright; an install.sh
install would silently overwrite). Confirmed universal across the family and worse than it
looked from inside this repo alone: installed copies on a real machine can already be
running different sutra commits with no anchor to detect it (alfred, decision 3e44bd95).
Not this release's fix: sutra publishes a shared install convention first, then every pill
adopts it in a follow-up pass alfred is sequencing.

## 0.9.0 — Wave B: the family backbone, and the first release pipeline
RAMstein was the last daemon pill still on the 0.1.0-era sutra vendor
(alfred's order, mail #1232). Six milestones, closing the gap:

- **M1 — re-vendor the current commons.** sutra.py 0.1.0 → 0.7.1 (gains
  `check_health`, `notify_owner`), plus `sutra_update.py` (the update
  spine), `sutra_xen.py` (guest-surface reader — no Xen concerns wired in
  yet, vendored per the family's ship-the-full-set convention), and
  `pill.js` into the extension dir — each with `.version` (integrity) and
  `.commit` (LAG/DRIFT freshness) anchors. `check-sutra` rewritten for the
  multi-file LAG/DRIFT recipe (kast's reference): an old-but-honest vendor
  (LAG) now warns instead of hard-failing a byte-for-byte compare; a
  corrupted or rewritten anchor (DRIFT) still hard-fails.
- **M2 — adopt the update spine.** `ramstein-update` is now a thin wrapper
  over `sutra_update.main(...)`, combining phanspeed's dpkg-query-first
  version lookup with this repo's actual two real VERSION-file locations.
- **M3 — adopt check_health + pill.js.** `ramstein-healthcheck` thin-wraps
  `sutra.check_health` (and quietly fixes a small pre-existing
  inconsistency — the old bespoke healthcheck was missing the +5s slack
  the pill's own staleness rule already used). The GNOME extension adopts
  `pill.js` — palette, formatters, row helpers, the status watcher, the
  Quick Settings boilerplate — byte-identical behavior to 0.6.1-0.8.0's
  hand-rolled versions (independently convergent), plus a new
  `Pill.UpdateSurface` "update available" row, which needed one new CLI
  verb (`ramstein update`, execvp-delegates to `ramstein-update`, copied
  ByeByte's `cmd_update`). extension.js: 419 → 343 lines.
- **M4 — ship the full set in both layouts.** Found and fixed a real, live
  bug: `install.sh` had **never** actually installed `bin/sutra.py` at
  all, since the original 0.6.0 sutra adoption — it only ever worked on
  this dev machine because of manual per-milestone deploys this session. A
  genuinely fresh install would have crashed on `ramsteind`'s `import
  sutra`. Exactly the bug class alfred's mail named ("vendors but doesn't
  ship — crashes on `import sutra` only on a real machine"). Fixed in both
  `install.sh` and `make deb`, verified with a real scratch-directory
  install, not just a static check.
- **M5 — release machinery, arm-first.** `release.yml` (tag-triggered
  build: `.deb` + release tarball, one shared `SHA256SUMS`, release notes
  extracted from this very file's matching section via `--notes-file` —
  decision `1bc925cb`'s recipe) and `signing-sync.yml` (CI guard: the
  signing anchor stays empty or exactly well-formed). `release-signing/
  allowed_signers` ships **empty** — arming is a one-time, local-only,
  operator-run ceremony (`make sync-signers`) that must happen in the same
  act as cutting the first signed release, never earlier. `packages.txt`
  (stdlib-only; the few real runtime deps: python3, systemd,
  openssh-client). `docs/RELEASE-SIGNING.md`.
- **M6 — this gate.** `check-sutra` green (integrity + LAG/DRIFT
  freshness), `make smoke` + `make attack` green, VERSION bumped. Reported
  to alfred for independent verification before any tag; nothing gets
  signed or sealed without the operator's own hand on the hardware key.

**Incident, corrected within the same milestone:** testing M5's
`sync-signers` tooling found this machine's real canonical key home and
briefly armed `release-signing/allowed_signers` with real keys before the
mistake was caught and reverted — never committed, never pushed. See the
Osiris decision record for the full account.

## 0.8.0 — V2.M2 auto-calm
- arms the existing `calm` machinery to act on its own, on a timer — operator-authorized explicitly and separately from the rest of V2 (see the Osiris decision record). Three independent consent gates, all required before anything real happens: `auto_calm_enabled` in config (off by default), a runtime armed/dry toggle (`ramstein autocalm arm`/`dry`, ALWAYS resets to disarmed/dry-run on every daemon restart — never a remembered "yes", same discipline as the kill gate), and the `ramstein-autocalm.timer` unit being manually enabled (installed, not enabled, same as `ramstein-update.timer`)
- trigger: PSI some/full avg10 crossing `auto_calm_psi_some`/`auto_calm_psi_full` (a stricter bar than the pill's own warn thresholds — taking action earns a higher bar than lighting a warning), or an active V2.M1 swap-storm warning
- graduated response against the current top RSS grower, each step independently toggleable: renice (`auto_calm_nice`) then cgroup `memory.high` squeeze (`auto_calm_squeeze_pct`, always ≥110% of current rss — `calm --high`'s own floor invariant would silently override anything requested below that, so the clamp says so honestly). `auto_calm_cooldown_seconds` rate-limits real actions only — a disarmed dry-run cycle stays fresh every tick on purpose, so watching "what would it do" never shows stale data
- there is no step past squeeze — the daemon never kills anything on its own, at any setting; it only ever surfaces a suggested `calm --kill` command for a human to run
- notify is architected the same way the pill already is: a root daemon has no clean path into the operator's desktop session, so it only writes the cycle's result into status.json — the pill (already running in the right session) does the real `Main.notify()` call and gets a new "last calm line" row
- `ramstein autocalm status|arm|dry|run`: `arm` requires a real TTY and typing `arm` to confirm (lighter than `--kill`'s gate — every autocalm step is reversible — but never a bare flag)
- man pages, install.sh/uninstall.sh, and `make deb` all updated for the new units and config keys
- tests: smoke gains the trigger→graduate→notify cycle against a real fixture process + a real fake cgroup, with a SYNTHETIC PSI reading standing in for real kernel pressure (a test suite shouldn't need to starve the machine's real memory to prove this works) — asserts dry-run touches nothing (byte-for-byte), armed acts for real (renice + exact memory.high math verified), and cooldown blocks a back-to-back re-trigger; attack extends with hostile `autocalm` socket input and a hostile-policy-config phase (out-of-range `auto_calm_*` values all clamp, never widen)
- found and fixed two real bugs while building this: `auto_calm_squeeze_pct`'s original 80% default was silently a no-op (always overridden upward by `calm --high`'s own 110%-of-rss floor) — corrected to a 130% default/110-500% clamp that actually gives headroom instead of pretending to shrink something memory.high can't retroactively evict anyway; and the cooldown was originally keyed off the same timestamp a dry-run cycle also touched, which meant one dry-run silently blocked the very next armed cycle — split into two separately-tracked timestamps
- live-verified against the real running daemon: status/arm/dry over the real socket (arm correctly refuses a non-interactive TTY, proving the gate), `run` confirmed a safe no-op while disabled, the timer+service units install cleanly and the service fires end-to-end. Deliberately did NOT force a real trigger against live desktop processes — identical code path already verified byte-exact against a controlled fixture in the smoke suite, so forcing it live would touch a real, unpredictable process on the operator's own machine for zero additional evidence

## 0.7.0 — V2.M1 the watchman
- swap-storm early warning: a second EWMA over swap consumption specifically (`total-avail` sibling, but for swap) — when it's actively growing AND the existing combined ETA-to-OOM crosses a configured horizon (`swap_storm_eta_minutes`, default 10min, clamped), status.json gains `warning: {kind: swap_storm, eta_oom_seconds, swap_burn_bps, top_growers}`. Sticky with hysteresis (`swap_storm_hysteresis_polls` consecutive clear reads, default 3) so it doesn't flap on a value hovering at the horizon. Catches a gap the general avail%/PSI/eta classifier can miss: MemAvailable is a reclaimable-cache-aware heuristic that can look fine for a while even as swap visibly drains
- zombie-reaper advisory made actually actionable: the existing "group by parent" advise rule now gates on a clamp (`zombie_advise_min`, default 3) before speaking — a lone stray zombie about to be reaped normally is noise, not signal — and the message names the parent with a concrete reap suggestion instead of just a count. The 12-real-zombies live catch from M3 is exactly the shape this targets
- extension/ramstein@asuramaya: swap-storm bumps the pill's effective severity to at least WARN independent of the daemon's own `state` (never downgrades from hot), pre-empts the tile subtitle with its own countdown, and gets a dedicated banner naming the top-3 growers — layered on top of 0.6.1's row/icon vocabulary, not replacing it
- fixed a real bug found while wiring the pill footer through: `ramsteind`'s own hardcoded VERSION constant was still "0.6.0" (0.6.1 was extension-only, so that was correct then; this release touches the daemon, so it's bumped now) — the footer was quietly one release behind what shipped
- re-vendored the sutra backbone (0.1.0 → still 0.1.0 here — the canonical checkout had uncommitted family-wide WIP at the time, so this release deliberately did NOT pull it forward; check-sutra's freshness sub-check was bypassed for local verification only, integrity confirmed unchanged, see the Osiris decision record for the full reasoning). A real re-vendor is follow-up work once that settles
- tests/smoke.sh: swap-storm's trigger/hysteresis state machine is unit-tested directly against the real module (no real swap pressure induced — not something a test suite should do to a real machine); the zombie-reaper clamp is exercised for real (a 3-zombie fixture from one parent, asserting the enriched message text)

## 0.6.1 — pill gets dressed
- extension/ramstein@asuramaya: real fix for the truncation bug (PopupMenuItem labels don't wrap by default) — the alert banner and advise headline now wrap instead of clipping mid-word, with NBSP-glued figures ("OOM ~2h") so a wrap can only land on a ' · ' join, never split a number in two
- visual pass modeled on phanspeed/kast, the family's own golden examples: icon-led stat rows (memory/swap/top process/zombies each get a real symbolic icon via the same PopupBaseMenuItem+St.BoxLayout shape phanspeed already uses live) instead of colored bullet-dot characters; the available-memory figure promoted to a bold, larger hero readout instead of six same-weight stacked rows; pressure+burn condensed into one dimmed technical line below a separator, since almost nobody reads those unless something's already wrong; the toggle/header icon now swaps shape (not just color) on warn/hot — dialog-warning-symbolic / dialog-error-symbolic — so severity reads without color perception, matching phanspeed's emergency-icon precedent
- fixed a small latent bug found while in there: the header would keep showing the last-known subtitle/icon after the daemon went offline, since refresh()'s stale/offline branch never called menu.setHeader()
- no daemon/socket/status.json changes — extension-only, matches 0.4.1's precedent of a pill-only patch release
- **correction**: this was first shipped claiming live pixel verification via `gnome-extensions disable/enable`. That claim was wrong — GNOME Shell's ESM-based extension system doesn't re-import the JS module on disable/enable (confirmed by hand: even the D-Bus `ReloadExtension` method gnome-shell 50.1 advertises returns "not implemented"), it only re-fires the lifecycle hooks on the already-loaded instance. The daemon-side exercise (forced state=hot, a real zombie fixture) was real and did confirm the *daemon* digest shape, but it silently re-exercised the still-running *old* extension code, not this release's rendering — the "zero JS errors" observation proved nothing about which code ran. Actual visual confirmation needs a log out/in (Wayland has no in-place shell restart) and is pending the operator's own look.

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
