# Release signing

Status: **unarmed** — `release-signing/allowed_signers` ships empty, as it
must until the operator's first sealed RAMstein release (Wave B, this
milestone). Once armed, it holds the operator's 4 canonical FIDO2 pubkeys,
same identity every other pill in the family uses.

## Why this exists

The SHA256 check RAMstein already does (`ramstein-update`'s manifest
verification) proves a download wasn't corrupted or truncated in transit.
It proves nothing about *authenticity*: the checksum comes from the same
GitHub release it's checking, so a compromised release asset carries its
own "valid" checksum. Closing that gap needs a signature from a key that
lives outside GitHub's control entirely.

## Mechanism: SSH signatures, FIDO2 hardware key

Chosen over GPG/minisign: SSH signature verification (`ssh-keygen -Y sign` /
`-Y verify`) is already in every OpenSSH install, needs no new dependency on
either side, and — the reason for the FIDO2 requirement — supports
**resident, touch-required hardware keys** (`ecdsa-sk` / `ed25519-sk`). The
private key material never leaves the hardware token, and every signature
needs a physical touch. A compromised CI runner or build machine cannot
forge a release; it would need the physical key in hand. This is the same
trust anchor the fleet's `rotten-apple` master-identity ceremony
established (2026-07-16) — RAMstein reuses that identity rather than
minting its own (per-project keys were the ruled-out footgun — see
`~/code/REPOS/RELEASE.md`).

**The signing key must never be provisioned into CI.** That's the whole
point — CI compromise is exactly the threat this defends against. Releases
are signed by hand, from the operator's own machine, with the hardware key
attached.

## Identity vs role — principal is WHO, namespace is WHAT-FOR

Per `~/code/REPOS/RELEASE.md` (the fleet's release doctrine): **principal**
(`-I`) is the repo's stable identity (`ramstein`); **namespace** (`-n`) is
what a given signature authorizes (`ramstein-release`). Never pass the same
string for both. `allowed_signers` line format (one line per key, exactly 4
when populated):

```
ramstein namespaces="ramstein-release,pills-tag" sk-ssh-ed25519@openssh.com <b64> ra-master-<n>
```

## One-time setup — `make sync-signers`, never hand-edit

```sh
make sync-signers
```

Rebuilds `release-signing/allowed_signers` from all 4 canonical pubkeys in
`~/.ssh/asuramaya-master/*.pub` (the operator's own key home; override with
`KEY_HOME=/path/to/asuramaya-master`). Always a full rebuild, never an
append. Refuses to run unless it finds exactly 4 canonical keys.

Unlike kast/phanspeed, there is no embedded `RELEASE_ALLOWED_SIGNERS` twin
inside `install.sh` to keep in sync: RAMstein's `install.sh` has no
curl-pipe bootstrap at all — it deliberately only ever runs from a local
checkout (see its own header comment), so there is nothing to verify
before a vendored `sutra_update.py` already exists to delegate to. Should
RAMstein ever grow a verified-release bootstrap, that convergence work
would land then, not speculatively here.

**Sequencing rule (do not skip):** `make sync-signers` populates the
anchor. Run it ONLY in the same act as cutting the operator's first signed
RAMstein release — arming it any earlier bricks `ramstein update` against
every existing unsigned release. Until then, `release-signing/
allowed_signers` ships empty and CI's `signing-sync` check
(`.github/workflows/signing-sync.yml`) just confirms that stays true (or,
once armed, that the anchor is exactly 4 well-formed lines).

## Per-release signing (operator, needs the FIDO2 key attached + a touch)

```sh
# Sign the checksum manifest, not each artifact — SHA256SUMS covers every
# release artifact (the tarball and the .deb) via its checksum entries, so
# signing it transitively covers the whole release, and it's tiny (one
# line per artifact).
ssh-keygen -Y sign -f /path/to/id_asuramaya_master_N.pub -n ramstein-release \
  SHA256SUMS
# -> produces SHA256SUMS.sig

gh release upload vX.Y.Z SHA256SUMS.sig
```

## Verification (client side — already built, M2)

```sh
sha256sum -c SHA256SUMS                                   # artifact matches the manifest
ssh-keygen -Y verify -f release-signing/allowed_signers \
  -I ramstein -n ramstein-release -s SHA256SUMS.sig \
  < SHA256SUMS                                             # manifest carries the operator's hand
```

Exit 0 = valid signature from the pinned principal, over exactly those
checksum bytes. Anything else is a hard failure. `ramstein-update` (a thin
wrapper over the family's shared `sutra_update.py`) already implements
this: `verify_dir()` checks whether the pinned anchor
(`ANCHOR_CANDIDATES` in `bin/ramstein-update`) carries any real key line —
blank means unarmed, and verification degrades to sha256-only with a
warning; once armed, a missing or non-verifying `SHA256SUMS.sig` is a hard
refusal, no install.

## Where the anchor lives once installed

`ramstein-update`'s `ANCHOR_CANDIDATES` checks, in order: `/usr/share/
ramstein/allowed_signers` (`.deb`), `/usr/local/share/ramstein/
allowed_signers` (`install.sh`'s source install), then the repo-relative
path (a dev checkout run in place). Both `install.sh` and `make deb` ship
a persistent copy of `release-signing/allowed_signers` to the installed
prefix — whatever it says at build/install time, empty or armed.

## `.deb`

`make deb` builds `build/deb/ramstein_<ver>_all.deb` (never installs it —
see `tests/smoke.sh`) and a matching `build/deb/SHA256SUMS`; `release.yml`
folds the release tarball's own checksum into the same manifest and
publishes both under one signature.

## Vendored commons (Wave B)

`bin/sutra.py` + `bin/sutra_update.py` + `bin/sutra_xen.py` (+ their
`.version`/`.commit` anchors) and `extension/ramstein@asuramaya/pill.js`
are vendored, never hand-edited — `make check-sutra` is the drift guard
(integrity always; freshness as a three-way LAG/DRIFT read against the
canonical `~/code/REPOS/sutra` checkout when present), wired into `make
smoke` and CI. RAMstein vendors the full set even though `sutra_xen.py`
isn't imported anywhere yet (no Xen guest-surface concerns wired in) — the
family's ship-the-full-set convention, so a future adoption never hits the
"vendored but not shipped" bug this same Wave B fixed for the other three
files (see `install.sh`'s and `make deb`'s own history).
