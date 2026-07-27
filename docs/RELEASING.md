# Releasing RAMstein

How a version becomes a signed release. The trust chain itself is described in
[RELEASE-SIGNING.md](RELEASE-SIGNING.md); this is the running order.

Two people are involved and only one of them can finish it. A maintainer prepares and tags. The
operator signs, by hand, with a physical FIDO2 key. No automation can stand in for that step, and
the signing key never goes near CI.

## 1. Prepare

Bump `VERSION` (root today; `packaging/VERSION` once the tree pass lands). It is the one version
constant, and `release.yml` asserts it matches the tag and `ramsteind`'s own internal VERSION
constant.

Write the `CHANGELOG.md` entry for the release. Small, focused releases are easier to sign off on
than a pile of unrelated changes.

Run the checks:

```
make smoke            # boot the daemon against fixtures, assert status.json shape
make attack            # fuzz the control socket adversarially, no root
```

`make check-sutra` runs as part of `make smoke` already. RAMstein has not yet adopted a `make
check` aggregate; that is expected to land alongside the repo's tree pass, following coldspot's
pattern.

## 2. Tag and publish

```
git tag vX.Y.Z && git push origin vX.Y.Z
```

`release.yml` then builds the `.deb`, a source tarball, and `SHA256SUMS`, extracts the matching
`CHANGELOG.md` section, and publishes the release unsigned. It signs nothing, on purpose: if CI
could sign, whoever compromised the workflow or the account could sign whatever they pushed, and
the anchor would be protecting nothing.

## 3. The operator seals it

```
make sync-signers     # only in the same act as a signing ceremony, see RELEASE-SIGNING.md
ssh-keygen -Y sign -f /path/to/id_asuramaya_master_N.pub -n ramstein-release SHA256SUMS
gh release upload vX.Y.Z SHA256SUMS.sig
```

This runs through the family's seal desk in practice, which derives its queue from published
releases and shows anything published without a `.sig` as awaiting the seal.

## Where RAMstein actually stands

v0.9.0 was tagged and published before the anchor held any real keys, so its shipped copy of
`release-signing/allowed_signers` is empty and every client verifying it degrades to sha256-only.
The anchor was armed with all 4 canonical keys immediately after, at HEAD. **The next tagged
release is the first one where the shipped anchor is real**, and from that release onward
`ramstein-update` enforces a valid signature and refuses to install an unsigned one. Treat every
release from here on the way coldspot already treats all of its own: sealed before anyone relies
on it.

## Rules that don't bend

* A sealed release is never re-cut. If something is wrong with it, the fix is the next version.
  Re-cutting breaks every copy that already verified it.
* The signing key never enters CI, in any form, for any reason.
* `make sync-signers` is not part of day-to-day dev. It rebuilds the trust anchor from the
  canonical keys and only ever runs in the same act as a signing ceremony.

## When it goes wrong

**The tag assertion fails** means `VERSION` and the tag disagree, or `ramsteind`'s own internal
VERSION constant is stale. Fix it, delete the tag, tag again.

**A client reports "armed but release is unsigned"** means the release was published and never
sealed. Nothing is broken in the artifact; it needs the operator's signature uploaded.
