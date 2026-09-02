#!/usr/bin/env python3
"""
Tests advise rule 7 (shared-memory visibility): ramstein's status/classify
correctly excludes Shmem from MemAvailable, per the kernel's own
accounting, but that meant the card stayed silent about it even when it
was the single largest reclaimable block of RAM on a machine (found
2026-08-02, Werner/alfred).

SPLIT-BEFORE-HANDOFF (alfred's ruling, 2026-09-02, msg 6394): the original
wording pointed at byebyte for the WHOLE shmem number, but byebyte is a
path-indexing tool and roughly half of a real machine's Shmem is memfd/
anonymous shared segments with no filesystem path at all (live-measured:
4.17G tmpfs-reachable of 9.22G total, alfred's msg 6386) -- a referral
covering 45% of a number while reading as though it covers all of it.
When `_tmpfs_reachable_bytes()` can measure the split, the rule now names
both halves and never claims byebyte can itemise the unpathed part.
When it can't (unavailable, or inconsistent with Shmem's own snapshot),
it falls back to the original undifferentiated, deliberately SYSTEM-WIDE
wording -- never implying /tmp specifically (Werner, DM #3228) and never
fabricating a split it couldn't actually measure (practice 2c45d78e).

Run as: python3 tests/test_shmem_advise.py
"""
import atexit
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

# query_advise touches the proc_stats index (rule 2/4) -- point it at a
# throwaway, empty state dir rather than the real /var/lib/ramstein.
_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-shmem-advise-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

# query_advise's rule 2 (growth) queries proc_stats directly -- the
# schema only exists once a Sampler has been constructed, same as the
# real daemon does at startup before anything else touches the index.
ramsteind.Sampler(dict(ramsteind.DEFAULTS))

_ORIG_TMPFS_FN = ramsteind._tmpfs_reachable_bytes


def _fake_status(shmem_pct):
    total = 61 * 1024**3
    return {"memory": {
        "total": total, "available": int(total * 0.5),
        "swap_total": 0, "swap_free": 0,
        "shmem": int(total * shmem_pct / 100.0),
        "psi": {"some_avg60": 0.0}, "eta_oom_seconds": None,
    }}


def _shmem_line(cfg, shmem_pct, tmpfs_bytes_fn):
    """tmpfs_bytes_fn stands in for the real /proc/mounts scan -- the real
    filesystem's actual tmpfs usage is not deterministic test input, so
    every case here controls it explicitly rather than reading it live."""
    ramsteind._tmpfs_reachable_bytes = tmpfs_bytes_fn
    try:
        lines = ramsteind.query_advise(cfg, lambda: _fake_status(shmem_pct))["lines"]
    finally:
        ramsteind._tmpfs_reachable_bytes = _ORIG_TMPFS_FN
    return next((l for l in lines if l["rule"] == "shmem"), None), lines


def main():
    fails = []
    cfg = dict(ramsteind.DEFAULTS)

    # below the default threshold (15%) -> rule must not fire
    line, lines = _shmem_line(cfg, 5.0, lambda: 0)
    if line is not None:
        fails.append(f"shmem rule fired below threshold: {lines}")

    # above threshold, tmpfs measurement succeeds and is consistent with
    # Shmem (tmpfs <= shmem) -> the SPLIT branch, naming both halves.
    total = 61 * 1024**3
    shmem_bytes = int(total * 25.0 / 100.0)
    tmpfs_bytes = int(shmem_bytes * 0.45)  # the live-measured ~45% shape
    anon_bytes = shmem_bytes - tmpfs_bytes
    line, lines = _shmem_line(cfg, 25.0, lambda: tmpfs_bytes)
    if line is None:
        fails.append(f"shmem rule did not fire above threshold: {lines}")
    else:
        text = line["text"]
        want_tmpfs = f"{tmpfs_bytes / 1024**3:.1f}G"
        want_anon = f"{anon_bytes / 1024**3:.1f}G"
        if want_tmpfs not in text:
            fails.append(f"split line missing the tmpfs figure {want_tmpfs!r}: {text!r}")
        if want_anon not in text:
            fails.append(f"split line missing the anonymous figure {want_anon!r}: {text!r}")
        if "ramstein top" not in text:
            fails.append(f"split line doesn't point the unpathed remainder"
                          f" anywhere a reader could actually look: {text!r}")
        if "can show what's actually in it" in text:
            fails.append("split line kept the old overpromising phrasing"
                          " (byebyte cannot itemise the anonymous half): "
                          f"{text!r}")

    # tmpfs measurement UNAVAILABLE (/proc/mounts unreadable) -> honest
    # fallback, the original undifferentiated wording -- never a
    # fabricated split from a partial read.
    line, lines = _shmem_line(cfg, 25.0, lambda: None)
    if line is None:
        fails.append(f"shmem rule did not fire when tmpfs is unmeasurable: {lines}")
    else:
        text = line["text"]
        if "/tmp" in text:
            fails.append(f"fallback wording implies /tmp specifically, should"
                          f" stay system-wide (Werner, DM #3228): {text!r}")
        if "shared memory across the system" not in text:
            fails.append(f"fallback wording missing the system-wide framing: {text!r}")

    # tmpfs measurement INCONSISTENT with Shmem's own snapshot (a
    # plausible race: two separate kernel reads a moment apart can
    # disagree under load) -> same honest fallback, not a negative split.
    line, lines = _shmem_line(cfg, 25.0, lambda: shmem_bytes + 1)
    if line is None:
        fails.append(f"shmem rule did not fire on an inconsistent tmpfs read: {lines}")
    elif "shared memory across the system" not in line["text"]:
        fails.append(f"inconsistent tmpfs>shmem reading didn't fall back honestly:"
                      f" {line['text']!r}")

    # threshold is configurable and actually respected
    cfg_strict = dict(ramsteind.DEFAULTS, advise_shmem_pct=50.0)
    line, lines = _shmem_line(cfg_strict, 25.0, lambda: 0)
    if line is not None:
        fails.append("shmem rule ignored a raised advise_shmem_pct threshold")

    if fails:
        print("SHMEM ADVISE TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("shmem advise ok: fires above threshold, silent below it, threshold"
          " configurable; splits tmpfs vs anonymous/memfd when measurable"
          " without overpromising byebyte's reach, falls back to honest"
          " undifferentiated wording when the split can't be measured or"
          " disagrees with Shmem's own snapshot")


if __name__ == "__main__":
    main()
