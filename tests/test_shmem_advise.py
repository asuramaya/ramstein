#!/usr/bin/env python3
"""
Tests advise rule 7 (shared-memory visibility): RAMstein's status/classify
correctly excludes Shmem from MemAvailable, per the kernel's own
accounting, but that meant the card stayed silent about it even when it
was the single largest reclaimable block of RAM on a machine (found
2026-08-02, Werner/alfred). Wording is deliberately SYSTEM-WIDE, never
implying it's /tmp specifically -- /proc/meminfo's Shmem sums every
tmpfs mount plus SysV/POSIX shared memory into one number, a different
scope than a /tmp-only tool like ByeByte's `why` (Werner, DM #3228).

Run as: python3 tests/test_shmem_advise.py
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

# query_advise touches the proc_stats index (rule 2/4) -- point it at a
# throwaway, empty state dir rather than the real /var/lib/ramstein.
_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-shmem-advise-test-")
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

# query_advise's rule 2 (growth) queries proc_stats directly -- the
# schema only exists once a Sampler has been constructed, same as the
# real daemon does at startup before anything else touches the index.
ramsteind.Sampler(dict(ramsteind.DEFAULTS))


def _fake_status(shmem_pct):
    total = 61 * 1024**3
    return {"memory": {
        "total": total, "available": int(total * 0.5),
        "swap_total": 0, "swap_free": 0,
        "shmem": int(total * shmem_pct / 100.0),
        "psi": {"some_avg60": 0.0}, "eta_oom_seconds": None,
    }}


def main():
    fails = []
    cfg = dict(ramsteind.DEFAULTS)

    # below the default threshold (15%) -> rule must not fire
    lines = ramsteind.query_advise(cfg, lambda: _fake_status(5.0))["lines"]
    rules = {l["rule"] for l in lines}
    if "shmem" in rules:
        fails.append(f"shmem rule fired below threshold: {lines}")

    # above the default threshold -> rule must fire, with system-wide
    # wording (never claiming this is /tmp specifically)
    lines = ramsteind.query_advise(cfg, lambda: _fake_status(25.0))["lines"]
    shmem_line = next((l for l in lines if l["rule"] == "shmem"), None)
    if shmem_line is None:
        fails.append(f"shmem rule did not fire above threshold: {lines}")
    else:
        text = shmem_line["text"]
        if "/tmp" in text:
            fails.append(f"wording implies /tmp specifically, should stay"
                          f" system-wide (Werner, DM #3228): {text!r}")
        if "shared memory across the system" not in text:
            fails.append(f"wording missing the system-wide framing: {text!r}")

    # threshold is configurable and actually respected
    cfg_strict = dict(ramsteind.DEFAULTS, advise_shmem_pct=50.0)
    lines = ramsteind.query_advise(cfg_strict, lambda: _fake_status(25.0))["lines"]
    if any(l["rule"] == "shmem" for l in lines):
        fails.append("shmem rule ignored a raised advise_shmem_pct threshold")

    if fails:
        print("SHMEM ADVISE TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("shmem advise ok: fires above threshold, silent below it,"
          " threshold configurable, wording stays system-wide not /tmp-specific")


if __name__ == "__main__":
    main()
