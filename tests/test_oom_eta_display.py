#!/usr/bin/env python3
"""
Tests the ETA-presentation fix (alfred's ruling, 2026-09-02, msg 6394):
eta_oom_seconds = headroom / a smoothed-but-still-noisy burn rate
(sutra.ewma_rate) is a straight division whose error amplifies
nonlinearly as the rate wobbles -- live-observed swinging 61m -> 69m
inside 30 seconds under a churning workload. Exact-minute display
("~61m") claimed a precision the estimator provably doesn't have.

Covers three things:
  1. ramstein CLI's human_oom_eta buckets the 2m-2h range instead of
     showing exact minutes, and specifically collapses BOTH sides of the
     observed 61m/69m swing into the same stable bucket.
  2. human_eta (the OTHER five callers -- sample age, blame span, kills
     age, autocalm age -- all exact elapsed-time subtractions with no
     estimator noise) is UNCHANGED -- coarsening those would be a
     regression, not a fix, so this negative-controls that human_oom_eta
     is a genuinely separate function, not a shared one with a flag that
     could leak.
  3. ramsteind's own advise rule 6 (_eta_bucket_text) agrees with the
     CLI's bucket boundaries, so the daemon's advise text and the CLI's
     status line never disagree about how confident to sound.

Run as: python3 tests/test_oom_eta_display.py
"""
import importlib.machinery
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIN_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramstein")
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ramstein = _load("ramstein_cli", RAMSTEIN_PATH)
ramsteind = _load("ramsteind_for_eta_test", RAMSTEIND_PATH)


def main():
    fails = []

    # THE OBSERVED INCIDENT ITSELF: 61m (3661s) and 69m (4140s) must land
    # in the identical bucket -- that's the whole fix, made concrete.
    sixty_one = ramstein.human_oom_eta(3661)
    sixty_nine = ramstein.human_oom_eta(4140)
    if sixty_one != sixty_nine:
        fails.append(f"the observed swing still shows differently:"
                      f" 61m={sixty_one!r} 69m={sixty_nine!r}")
    if sixty_one != "<2h":
        fails.append(f"expected both in the <2h bucket, got {sixty_one!r}")

    # boundary table -- every edge, both sides.
    cases = [
        (None, "—"), (0, "~0s"), (45, "~45s"), (119, "~119s"),
        (120, "<5m"), (299, "<5m"), (300, "<15m"), (899, "<15m"),
        (900, "<30m"), (1799, "<30m"), (1800, "<1h"), (3599, "<1h"),
        (3600, "<2h"), (7199, "<2h"), (7200, "~2h"), (7201, "~2h"),
        (10800, "~3h"), (2 * 86400, "~2d"), (3 * 86400, "~3d"),
    ]
    for seconds, want in cases:
        got = ramstein.human_oom_eta(seconds)
        if got != want:
            fails.append(f"human_oom_eta({seconds}) = {got!r}, want {want!r}")

    # NEGATIVE CONTROL: human_eta (the generic exact-duration formatter,
    # used for sample age/blame span/kills age/autocalm age) must NOT be
    # touched by this fix -- it still shows exact minutes, since those
    # are plain elapsed-time subtractions with no estimator noise.
    exact_cases = [(3661, "~61m"), (4140, "~69m"), (45, "~45s")]
    for seconds, want in exact_cases:
        got = ramstein.human_eta(seconds)
        if got != want:
            fails.append(f"human_eta({seconds}) = {got!r}, want {want!r}"
                          f" (generic duration formatter regressed by the"
                          f" ETA-only fix)")

    # THE DAEMON'S advise rule 6 must agree with the CLI's bucket
    # boundaries below 1h (the only range rule 6 ever renders, since its
    # own guard is eta < 3600) -- same underlying claim, must not disagree.
    daemon_cases = [
        (45, "45s"), (119, "119s"), (120, "under 5m"), (299, "under 5m"),
        (300, "under 15m"), (899, "under 15m"), (900, "under 30m"),
        (1799, "under 30m"), (1800, "under 1h"), (3599, "under 1h"),
    ]
    for seconds, want in daemon_cases:
        got = ramsteind._eta_bucket_text(seconds)
        if got != want:
            fails.append(f"ramsteind._eta_bucket_text({seconds}) = {got!r},"
                          f" want {want!r}")

    if fails:
        print("OOM ETA DISPLAY TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("oom eta display ok: human_oom_eta collapses the observed 61m/69m"
          " swing into one stable bucket, every boundary correct, the"
          " generic human_eta duration formatter is untouched, and the"
          " daemon's advise-rule wording agrees with the CLI's buckets")


if __name__ == "__main__":
    main()
