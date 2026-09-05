# Changelog

## 0.13.0 — the memory stance: from gauge to policy

The pitch changed (operator ruling, 2026-09-05): ramstein was a gauge on a system
that already has one — the kernel and `systemd-oomd` manage pages by pressure,
blind to intent. This release is ramstein keeping a short, operator-authored
policy applied as real cgroup memory controls, plus everything found closing
the honesty and visibility gaps that made the pitch possible in the first
place.

### The memory stance (V4)
- `/etc/ramstein/stance.json` — ships with **zero rules**: protects nothing,
  caps nothing until the operator names one. Never written by the daemon;
  `src/data/config/stance.example.json` installs alongside it as a reference
  the operator copies and edits by hand (protect osiris-pg and the agent
  fleet, expendable Chrome, cap containers at 25%, one sentence per rule).
- Three tiers, matched against **leaf** cgroup scopes only (`unit_glob`,
  `comm`, `exe_glob`, `container_glob`, first match wins; no match is
  `unclassified`, reported, never capped):
  - **protect** — `memory.low` on the target *and* every shared ancestor up
    through `user@<uid>.service`/`user-<uid>.slice`, sized to protected usage
    and capped at `stance_protect_floor_ceiling_pct` (50%) of MemTotal.
    Measured directly, before writing any of this: a leaf's own `memory.low`
    protects nothing against reclaim pressure from an ancestor whose own low
    is unset — real protection needs the whole chain written, not just the
    leaf.
  - **expendable** — no standing squeeze. `memory.high` rides `autocalm`'s
    own three consent gates and squeeze mechanism as one more step, applied
    only while the system is hot and released — self-releasing, unlike the
    pre-existing top-RSS squeeze — the instant it calms.
  - **cap** — a standing `memory.high`, fixed or percent-of-total.
  - A cgroup inside a delegated `user@<uid>.service` subtree is written via
    the same `sudo -u '#<uid>' env DBUS_SESSION_BUS_ADDRESS=...
    systemctl --user set-property` bridge the post-kill notification below
    already uses — measured directly: a raw write into a delegated subtree
    races the owning user manager. Everything else (`system.slice`, docker
    scopes) is root's own cgroup outright, no bridge needed.
- `ramstein ledger [--json]` — memory **by thing**, not by pid: same-tier
  scopes collapse into one line (count, resident vs cgroup-charged bytes,
  which road); `unclassified` groups by `exe` (falling back to `comm`) so an
  unnamed population still reads as one line. Also surfaces a synthetic
  `kernel` row naming the single scope carrying the most kernel slab
  (dentries/inodes) — `memory.current` already counts it toward that scope's
  charge, but nothing had ever named it; a real, measured multi-gigabyte
  finding on the operator's own machine.
- `ramstein stance status|rollback` — `status` shows the applied protect
  floor (and whether it's pinned at the ceiling) and how many scopes are
  capped; `rollback` walks every cgroup property ever written and resets
  each to its off value via the same road it was written with, reading it
  back to confirm. Runs automatically before the stance (re-)applies on
  every daemon start, and is the daemon's own response to a stance file that
  fails to load — a bad or unloadable stance applies nothing, ever.
- The GNOME pill's **Findings** fold: a new closed-by-default disclosure,
  its own label carrying the live finding (an incident citing which tier
  pushed back first, a protecting/expendable summary — naming its own
  "pinned at ceiling" basis when that's what's actually applied, "no stance
  — reporting only", or "nothing is holding dead memory"), its body the
  ledger itself. Existing Advanced controls are untouched.
- A ceiling **throttles and never kills** — measured directly against a
  disposable memory hog before any of this shipped: `memory.high` pins usage
  at its ceiling via continuous reclaim, zero OOM events, contained almost
  entirely to the capped scope. Kill remains exclusively a human TTY verb.

### Visibility, before the stance needed it
- `ramstein kills [--since T]` — kernel OOM-kill events (`journalctl -k`),
  a class of event `top`/`blame` structurally cannot see (a killed process
  is gone by the time either walks `/proc`).
- The shmem advise rule now honestly splits tmpfs-backed usage from true
  anonymous/memfd usage instead of one undifferentiated number, with a
  disclosed fallback when the split can't be measured; OOM-ETA display
  coarsens to buckets (a raw-seconds figure was overclaiming precision an
  EWMA burn-rate estimate can't support).
- A real desktop notification the instant the kernel OOM-kills something —
  straight over D-Bus to the owner's own session
  (`sudo -u '#<uid>' env DBUS_SESSION_BUS_ADDRESS=...  notify-send`), going
  around the GNOME pill entirely so it reaches the desktop even on a shell
  that hasn't reloaded in days. A burst collapses to one notification naming
  the most recent, never one popup per kill.
- `ramstein standing` — stock, not flow: what's large and has stopped
  moving (a flat-for-a-day process is furniture, not activity), the
  anonymous/memfd memory byebyte structurally cannot see, and the swap
  watermark (since-timestamp, peak-only-increases).
- `ramstein incidents [--limit N]` — fire marshal, not firefighter: a
  snapshot of the top residents by rss+swap the instant swap%, PSI, or an
  active swap-storm crosses a real threshold, so "who was resident when it
  happened" is answerable after the fact instead of only ever a bare
  watermark number.

### Release engineering
- `packaging/packages.txt` is now the single source of truth for the
  `.deb`'s `Depends`/`Suggests` — the control stanza is generated from it
  (Tantra's shared `sutra.mk` recipe) instead of being hand-duplicated in
  the `Makefile`, so the two can no longer silently drift.
- `release.yml` now corrects a release's body to the CHANGELOG section even
  when the release already existed before this run (a prior manual creation
  or partial run could otherwise leave GitHub's default compare-stub body
  in place forever, since asset re-upload alone never touches it).

### Hardening, found by the first real install
Alfred's own install of the stance on the operator's box surfaced three real
bugs no amount of mocked testing had caught, plus one deliberate sizing
change:
- **The classifier bug.** A scope classifies by what it *is*, never by an
  incidental co-resident process. `_match_leaf` rewritten from a single
  first-match-wins pass to two: pass one checks `unit_glob`/`container_glob`
  (the scope's own name) against every rule before any per-process match is
  even considered; pass two checks `comm`/`exe_glob` against the *dominant*
  process only (by resident bytes), never any process. The live bug: Chrome's
  own app scope, holding two Chrome processes and one claude-in-chrome-host
  process, classified as `protect` (the fleet's `exe_glob` rule, listed
  first) instead of `expendable` (Chrome's own `unit_glob` rule, listed
  second) — the plan wrote `MemoryLow=1.4G` on the operator's own browser,
  the opposite of the stance.
- **The ledger mislabeling bug.** Unclassified scopes now group by their
  *dominant* process's exe (by resident bytes), with a new `others` count
  for the rest, instead of an arbitrary first-enumerated pid — the live
  case: a 21-Chrome-process scope reading as "1 cat", a 44-process fleet
  terminal reading as one stale version string.
- **The rollback false-success bug.** `stance rollback` now refuses loudly
  (rather than reporting "0 entries, complete") when it cannot confirm the
  touched-cgroups list is genuinely empty versus merely unreadable —
  distinguishing a missing file (honest, nothing was ever touched) from any
  other read failure (permission, corruption — report nothing as done).
- **The protect floor now sizes on resident bytes, not charged.** A
  protect-tier leaf's `memory.low` floor sums every leaf's own *resident*
  memory, never `memory.current` (which also charges file cache) — sizing
  the floor on cache would let a scope's own page-cache churn inflate a
  reservation meant to hold working state. `memory.low` exists to keep the
  fleet's own working state warm, not to pin file cache the kernel could
  re-read in seconds: cache above the resident floor inside a protected
  scope stays reclaimable under pressure, and that's a performance cost
  only, the correct one. The **cap** tier's ceiling stays sized against
  charged usage, deliberately — it's a worst-case backstop, and a backstop
  must bound everything the kernel is holding. Every applied floor now
  carries its own basis in `stance status`/`plan` output (e.g. "resident of
  20 processes in 1 scope"), so the number is never reported bare — and it
  names processes and scopes, not "sessions": the live find, a single
  20-process scope reading as "resident of 1 session," which read as one
  fleet session protected when the truth was twenty processes in one leaf.
- `container_glob` now matches a docker container's own `--name`, not just
  its raw `docker-<id>.scope` cgroup name — resolved straight from
  dockerd's own `config.v2.json` (0700 root, exactly what `ramsteind`
  already runs as), with the raw scope name as a fallback when resolution
  fails. A container's own ID rotates every time `docker compose`
  recreates it; its name doesn't, so a rule written against the name
  survives a recreate that would silently break one written against the
  ID (alfred msg 7528, from his own read of dockerd's on-disk state). The
  shipped `stance.example.json` now protects `osiris-pg` by its real
  `container_glob: "osiris-pg"`, placed *before* the cap-all-docker rule —
  the two-pass classifier checks scope identity across every rule first,
  so a `container_glob` naming a container by exe alone can never win
  against a `container_glob` that matches every container. `ramstein
  ledger` and `stance plan`/`status` now label a classified docker leaf by
  its resolved name too (`"docker osiris-pg"` instead of a 64-hex ID), and
  two differently-named containers matching the same rule stay two
  distinct rows rather than blending into one under a bare rule number.

## 0.12.0 — layer 3: configuring the system, and its consent model

FAMILY.md's third layer ("configure the system", not just observe or act on
processes) is what ramstein was missing, and this release is what closes it:
four new daemon verbs, a pill control for every one of them, and a converged
consent model across all five.

### The single most consequential line in this release
**`ramstein swappiness set`, `swap-size set`, `zram enable`, `oomd enroll`,
and `autocalm arm` now require `--yes` to apply.** Every one of these used to
prompt interactively (`type 'set' to confirm`) and act on confirmation; they
now dry-run by default — printing what the action would do, without doing
it — and only apply with `--yes` on the command line. This was required to
make these verbs usable from the GNOME pill, which spawns no TTY, ever, and
matches the shape byebyte's `reserve`/`declare` already shipped. Nothing that
used to act now acts differently in a dangerous direction — a
non-interactive caller that used to be refused outright now gets a harmless
preview instead — but **anyone who types `set`/`enroll`/`enable`/`arm`
expecting the old typed-confirmation prompt will get a preview instead and
needs to add `--yes`.**

### Layer 3: four new verbs, plus autocalm's first real floor
- `oomd enroll`/`disenroll` — closes the gap where Ubuntu ships
  `systemd-oomd` configured to kill on memory pressure but never on
  sustained swap exhaustion, the exact scenario ramstein exists for. Refuses
  outright if oomd's own swap-kill trigger already holds.
- `swappiness status`/`set N`/`reset` — a sysctl.d drop-in for reboot
  survival plus an immediate live apply; the pre-ramstein value is ledgered
  once so `reset` always restores the true original.
- `swap-size status`/`set SIZE`/`remove` — a standalone, additive swap file
  with its own systemd `.swap` unit, genuinely BOUNDED-WAIT (`set` reports
  `pending`, the CLI/pill poll for the real outcome).
- `zram status`/`enable`/`disable` — compressed RAM-backed swap via
  `systemd-zram-generator`. New floor: refuses if `/dev/zram0` is already
  claimed by systemd's own auto-generated `dev-zram0.swap`, naming the
  holding unit, rather than resetting a device something else has active.
- `autocalm arm` — the repo's highest-authority verb (standing permission
  for ramsteind to renice and squeeze cgroup memory.high on its own, on a
  timer) had only ever had a TTY prompt as its gate. It now has a real one:
  refuses if the trigger condition (PSI or an active swap-storm warning) is
  already firing, since arming into it would hand the very next scheduled
  tick an unreviewed action.

### The pill: from readout to control surface
Five controls: swap-size (BOUNDED-WAIT preset chips), oomd/zram (TOGGLEs),
swappiness (a 3-stance SEGMENT — Avoid swap/Balanced/Favor swap), and
autocalm arm (a real preview-then-confirm flow, not a plain toggle, given
what it grants). The daemon's status digest (`build_pill_summary`) was
extended to carry all four configuration verbs' state so the pill never
needs its own socket client, just the one status.json it already watches.

Found building this: `request_or_die`'s blanket exit-on-error swallowed
stdout for every `--json` caller, so a real refusal (an out-of-range value,
a preflight refusal, a failed write) was indistinguishable from "daemon
unreachable" to the one caller — the pill — that most needs to tell them
apart. Fixed by porting the same `as_json` shape byebyte had already shipped
for the identical gap.

### Correctness fixes found along the way
- Three separate `ReadWritePaths` gaps under the hardened unit's
  `ProtectSystem=strict` — each new layer-3 verb touched a path the unit
  file hadn't been told to allow, and a missing target crash-loops the
  *whole* daemon, not just one write.
- The oomd coexistence check now verifies actual enrollment via `oomctl`,
  not just `systemctl is-active` — it had been reporting swap-kill
  protection that did not exist.
- `swap-size`'s backing file: fixed a sparse-file bug (`os.truncate`
  produces holes; `mkswap`/`swapon` both refuse them) and a `.swap` unit
  misnaming bug (systemd requires the escaped form of its own `What=`).
- `zram enable` now stops a pre-existing device before reconfiguring —
  `systemctl start` on an already-active oneshot unit is a no-op, so the
  package's own stock default device could silently survive a "successful"
  reconfigure.
- A family-wide CI gap: `node --check <path>` silently skips real syntax
  validation on any file with a top-level `import` — every pill's own
  "GNOME extension (syntax)" check had validated nothing, ever, for GJS.
  Fixed here, relayed to every other pill in the family.
- Dependency discipline: `zram`/`gnome-shell` are `Suggests`, never a hard
  `Depends` — nothing this package installs should auto-pull a desktop
  environment onto a headless box.

### Renamed
RAMstein → ramstein, text-only, family-wide convention (the mixed-case name
was "a fatal error at inception" — operator). No file paths changed; they
were already lowercase.

### Also
- New advise rule: the card stayed silent about Shmem even on a night it was
  the single largest reclaimable block of RAM on the machine.
- `make test`: one true entrypoint (`smoke` + `attack`, globbing
  `tests/test_*.py` instead of a hand-maintained list that had already
  started drifting), so `python3 -m pytest tests/` — which silently
  misreports these standalone scripts as passing — is never mistaken for
  the real signal.
- Fixed a real leaked-tmpdir bug in `test_shmem_advise.py`.

Verified: `make check` and `make test` (`smoke` + `attack`) both green on
the real repo; CI green on the tagged commit, checked per-job, not by
summary alone.

## 0.11.1 — sutra.mk / pill-ci.yml adoption
Structural only, no daemon/CLI/pill behavior changed. ramstein piloted the family's shared
recipe layer ahead of the other four pills, so they could copy this diff rather than a
description of it (alfred's order, DM #2716).

- Vendored `sutra.mk` (the Makefile fragment sutra now publishes alongside `sutra.py`/
  `sutra_update.py`/`sutra_xen.py`) and replaced the hand-written `check-sutra` target and
  `check-repo`'s row-count logic with it. Found and reported upstream: the original loop never
  covered `pill.js` (a gap for 3 of 5 pills), `check-vendored-path` validated only one binary
  per call (ramstein has four), `pill-ci.yml` shellchecked nothing, `run-check-version`
  defaulted on with no pill actually using it, and `SUTRA_CHECK_ARGS` defaulting to `--help`
  made `make check` place a real (harmless, read-only) call against the live daemon socket on
  every run. All five folded upstream into sutra 0.11.0/0.11.1.
- Adopted `pill-ci.yml` (sutra's shared reusable CI workflow), pinned by commit SHA. Split
  ramstein's CI into the shared job plus a thin `ramstein-specific` sibling for the one thing
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
  gate, copied from coldspot. Added .gitattributes: ramstein had none, so CI/dev files were
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
ramstein was the last daemon pill still on the 0.1.0-era sutra vendor
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
  byebyte's `cmd_update`). extension.js: 419 → 343 lines.
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
- vendored bin/sutra.py + bin/sutra.version (sutra 0.1.0, byebyte is the pilot extraction); ramsteind/ramstein now import it as a sibling instead of hand-rolling the same skeleton
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
