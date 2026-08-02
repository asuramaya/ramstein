#!/usr/bin/env python3
"""
Tests classify()'s state_evidence: FAMILY.md ("Headers state STATE, not a
number") -- where a state is derived from OR'd conditions, the header must
carry WHICH evidence fired. classify() ORs three independent conditions
(psi/avail/eta) at each of two levels (warn/hot); before this, the bare
"warn"/"hot" string thrown away which one(s) actually crossed a threshold,
so "warn from stalling tasks" and "warn from a three-hour ETA" -- genuinely
different situations needing different responses -- were indistinguishable
on the card (alfred, ruling: RAMstein's own header read "OOM ~11h" directly
above "pressure 0.0% . burn quiet", predicting death and reporting calm in
the same card with nothing tying the two together).

Fulcrum-standard negative control throughout: every "fires" assertion is
paired with a "does NOT fire on the adjacent condition" assertion, so a
classify() that always returned every code (or none) would fail this suite,
not silently pass it.

Run as: python3 tests/test_header_evidence.py
"""
import importlib.machinery
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

CFG = dict(ramsteind.DEFAULTS)
TOTAL = 100 * 1024**3  # round total so avail% arithmetic is exact


def _classify(avail_pct=50.0, some10=0.0, full10=0.0, eta=None):
    avail = int(TOTAL * avail_pct / 100.0)
    psi = {"some_avg10": some10, "full_avg10": full10}
    return ramsteind.classify(TOTAL, avail, psi, eta, CFG)


def main():
    fails = []

    def check(label, got, want_state, want_evidence):
        state, evidence = got
        if state != want_state or sorted(evidence) != sorted(want_evidence):
            fails.append(f"{label}: got ({state!r}, {evidence!r}), wanted"
                         f" ({want_state!r}, {want_evidence!r})")

    # baseline: nothing crosses anything -> ok, no evidence at all (not
    # just "no warn/hot label" but a genuinely empty evidence list)
    check("all calm", _classify(), "ok", [])

    # warn, each condition ALONE -- and NOT the other two, proving the
    # evidence list isn't just "whatever's non-default"
    check("warn via psi only", _classify(some10=15.0), "warn", ["psi"])
    check("warn via avail only", _classify(avail_pct=10.0), "warn", ["avail"])
    check("warn via eta only", _classify(eta=2 * 3600), "warn", ["eta"])

    # warn, two conditions at once -- both named, not collapsed to one
    check("warn via avail+eta", _classify(avail_pct=10.0, eta=2 * 3600),
          "warn", ["avail", "eta"])

    # hot, each condition ALONE
    check("hot via psi only", _classify(full10=10.0), "hot", ["psi"])
    check("hot via avail only", _classify(avail_pct=3.0), "hot", ["avail"])
    check("hot via eta only", _classify(eta=600), "hot", ["eta"])

    # hot, all three at once -- every fired condition named, none dropped
    check("hot via psi+avail+eta",
          _classify(full10=10.0, avail_pct=3.0, eta=600),
          "hot", ["psi", "avail", "eta"])

    # PRECEDENCE: hot short-circuits before warn is even evaluated -- a
    # some10 that would independently warn must NOT leak into hot's
    # evidence list when full10 alone already made it hot
    check("hot precedence doesn't leak warn-only evidence",
          _classify(full10=10.0, some10=15.0), "hot", ["psi"])

    if fails:
        print("HEADER EVIDENCE TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("header evidence ok: classify() names which OR'd condition(s)"
          " fired, at both warn and hot, singly and combined")


if __name__ == "__main__":
    main()
