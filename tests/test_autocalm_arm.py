#!/usr/bin/env python3
"""
Tests ramsteind's `autocalm arm` verb's retrofit to the dry-run/--yes
shape (alfred, msg 3450 -- the fifth verb, converged after the pill
fit-report's three plus swappiness, msg 3429). This is the highest-
authority verb in the repo: arming grants ramsteind standing permission
to renice and squeeze cgroup memory.high ON ITS OWN, on a timer, without
asking again -- and it PERSISTS across daemon restarts (config
auto_calm_armed).

THE FLOOR (named, not assumed, per alfred's "nothing to clamp doesn't
answer the question" standard): _autocalm_arm_preflight refuses arming
if do_autocalm_run's OWN trigger condition (PSI some/full over
threshold, or an active swap-storm warning) is ALREADY firing -- arming
into an already-firing trigger hands the very next scheduled tick an
immediate, unreviewed action, the identical shape of danger
_oomd_enroll_preflight exists to catch for oomd enrollment. Both
_autocalm_arm_preflight and do_autocalm_run share ONE trigger function
(_autocalm_trigger) so they cannot drift into checking slightly
different things.

Run as: python3 tests/test_autocalm_arm.py
"""
import atexit
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-autocalm-arm-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

CFG = dict(ramsteind.DEFAULTS)  # auto_calm_psi_some=20.0, psi_full=10.0, use_swap_storm=True


def _status(some10=None, full10=None, swap_storm=False):
    doc = {"memory": {"psi": {"some_avg10": some10, "full_avg10": full10}}}
    if swap_storm:
        doc["warning"] = {"kind": "swap_storm"}
    return doc


def _fresh_state():
    return {"armed": False}


def test_preflight_safe_when_nothing_firing():
    fails = []
    for name, doc in [
        ("no status at all", {}),
        ("psi present, well under both thresholds", _status(some10=1.0, full10=0.5)),
        ("psi some right at the threshold (not over)", _status(some10=20.0)),
    ]:
        got = ramsteind._autocalm_arm_preflight(CFG, lambda d=doc: d)
        if got is not None:
            fails.append(f"[{name}] expected safe (None), got: {got!r}")
    return fails


def test_preflight_refuses_when_already_firing():
    fails = []
    for name, doc, expect_substr in [
        ("PSI some over threshold", _status(some10=25.0), "PSI some"),
        ("PSI full over threshold", _status(full10=15.0), "PSI full"),
        ("swap storm active", _status(swap_storm=True), "swap storm"),
    ]:
        got = ramsteind._autocalm_arm_preflight(CFG, lambda d=doc: d)
        if got is None or expect_substr not in got:
            fails.append(
                f"[{name}] expected a refusal containing {expect_substr!r}, got: {got!r}")
        if got is not None and "no time to reconsider" not in got:
            fails.append(f"[{name}] refusal doesn't explain the actual danger: {got!r}")
    return fails


def test_preflight_and_run_share_one_trigger_function():
    """Proves the floor is load-bearing, not decorative, WITHOUT needing
    do_autocalm_run's full sampling-DB machinery (query_top's top-RSS
    selection is a separate subsystem, out of scope here): both
    _autocalm_arm_preflight and do_autocalm_run call the exact same
    _autocalm_trigger(cfg, doc) -- by construction, whatever condition
    makes the preflight refuse is IDENTICAL to whatever condition would
    make do_autocalm_run act on the very next tick, since there is only
    one function that decides. This is what rules out the two silently
    drifting into checking slightly different things over time."""
    fails = []
    for doc in (_status(some10=25.0), _status(full10=15.0), _status(swap_storm=True)):
        preflight_refused = ramsteind._autocalm_arm_preflight(CFG, lambda d=doc: d) is not None
        would_trigger = ramsteind._autocalm_trigger(CFG, doc) is not None
        if preflight_refused != would_trigger:
            fails.append(
                f"preflight and the run-time trigger disagree for {doc!r}:"
                f" preflight_refused={preflight_refused},"
                f" would_trigger={would_trigger}")
        if not would_trigger:
            fails.append(f"test fixture doc didn't actually trigger: {doc!r}")
    return fails


def test_dry_run_preview_does_not_mutate():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as f:
            json.dump({}, f)
        state = _fresh_state()

        # explicit dry_run=True
        r = ramsteind.do_autocalm_arm(CFG, lambda: _status(), state, config_path, dry_run=True)
        if not r.get("dry_run") or "note" not in r:
            fails.append(f"explicit dry_run=True didn't return a preview: {r!r}")
        if state["armed"]:
            fails.append("dry_run=True call armed the runtime state anyway")
        with open(config_path) as f:
            if json.load(f) != {}:
                fails.append("dry_run=True call wrote to config.json anyway")

        # THE SAFE DEFAULT (alfred's ratification, msg 3429, item 1):
        # omitting dry_run entirely must ALSO preview, never arm.
        r = ramsteind.do_autocalm_arm(CFG, lambda: _status(), state, config_path)
        if not r.get("dry_run"):
            fails.append(f"omitting dry_run did not default to a preview: {r!r}")
        if state["armed"]:
            fails.append("the default (no dry_run arg) call armed the runtime state anyway")
        with open(config_path) as f:
            if json.load(f) != {}:
                fails.append("the default (no dry_run arg) call wrote to config.json anyway")
    return fails


def test_real_arm_sets_state_and_persists():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as f:
            json.dump({"some_other_key": "untouched"}, f)
        state = _fresh_state()

        r = ramsteind.do_autocalm_arm(CFG, lambda: _status(), state, config_path, dry_run=False)
        if not r.get("ok") or not r.get("armed"):
            fails.append(f"real arm didn't report success: {r!r}")
        if not state["armed"]:
            fails.append("real arm didn't set the runtime armed flag")
        with open(config_path) as f:
            persisted = json.load(f)
        if persisted.get("auto_calm_armed") is not True:
            fails.append(f"real arm didn't persist auto_calm_armed=true: {persisted!r}")
        if persisted.get("some_other_key") != "untouched":
            fails.append(f"real arm clobbered an unrelated config key: {persisted!r}")
    return fails


def test_real_arm_refused_by_preflight_does_not_touch_anything():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as f:
            json.dump({}, f)
        state = _fresh_state()

        r = ramsteind.do_autocalm_arm(CFG, lambda: _status(some10=30.0), state,
                                      config_path, dry_run=False)
        if r.get("ok") or "error" not in r:
            fails.append(f"expected a refusal, got: {r!r}")
        if state["armed"]:
            fails.append("preflight refusal still armed the runtime state")
        with open(config_path) as f:
            if json.load(f) != {}:
                fails.append("preflight refusal still wrote to config.json")
    return fails


def main():
    all_fails = []
    for name, fn in [
        ("preflight safe when nothing firing", test_preflight_safe_when_nothing_firing),
        ("preflight refuses when already firing", test_preflight_refuses_when_already_firing),
        ("preflight and run share one trigger function (no drift possible)",
         test_preflight_and_run_share_one_trigger_function),
        ("dry-run preview does not mutate (explicit and default)",
         test_dry_run_preview_does_not_mutate),
        ("real arm sets state and persists", test_real_arm_sets_state_and_persists),
        ("real arm refused by preflight touches nothing",
         test_real_arm_refused_by_preflight_does_not_touch_anything),
    ]:
        fails = fn()
        if fails:
            all_fails.append((name, fails))
        else:
            print(f"autocalm arm: {name} ok")

    if all_fails:
        print("AUTOCALM ARM TEST FAILED:")
        for name, fails in all_fails:
            for f in fails:
                print(f"  - [{name}] {f}")
        sys.exit(1)
    print("autocalm arm ok: preflight floor (shared trigger logic, no drift"
          " possible), dry-run preview never mutates, real arm sets state"
          " and persists, a refused arm touches nothing")


if __name__ == "__main__":
    main()
