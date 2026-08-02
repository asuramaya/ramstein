#!/usr/bin/env python3
"""
Tests RAMstein's layer-3 swappiness verb (decision f8e7cc5a): SEGMENT
presets, CHEAP, PERSISTENT -- a sysctl.d drop-in for reboot survival plus
an immediate live apply, with the prior value ledgered before the FIRST
change so `reset` restores the true pre-RAMstein baseline rather than
some RAMstein-chosen number.

Fulcrum-standard negative control: a case where the "live apply" write
silently doesn't move the underlying value (a read-only fixture,
standing in for e.g. a hardened unit's write path not actually being
writable) must come back as ok=False naming the disagreement -- ruling
41b72476, "the pill's own observation must move, not just the file."

Run as: python3 tests/test_swappiness.py
"""
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")


def _fresh_module(tmp, initial_swappiness="60"):
    """A clean ramsteind import with all three env overrides pointed at
    fixtures inside `tmp` -- state dir (prior-value ledger), sysctl.d root
    (persistent drop-in), and the proc-swappiness file itself (live
    apply). All three are honored ONLY because we're unprivileged, same
    guarantee the daemon gives a real root process."""
    state_dir = os.path.join(tmp, "state")
    sysctl_root = os.path.join(tmp, "etc")
    proc_path = os.path.join(tmp, "proc_swappiness")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(sysctl_root, exist_ok=True)
    with open(proc_path, "w") as f:
        f.write(initial_swappiness)

    os.environ["RAMSTEIN_STATE_DIR"] = state_dir
    os.environ["RAMSTEIN_SYSCTL_ROOT"] = sysctl_root
    os.environ["RAMSTEIN_PROC_SWAPPINESS"] = proc_path

    loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
    spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, proc_path, sysctl_root


def main():
    fails = []

    # --- fresh machine: status, range validation, a clean set ---------------
    with tempfile.TemporaryDirectory() as tmp:
        m, proc_path, sysctl_root = _fresh_module(tmp)

        status = m.query_swappiness_status()
        if status != {"current": 60, "ramstein_dropin_present": False, "prior": None}:
            fails.append(f"fresh status wrong: {status!r}")

        # range validation: rejected BEFORE anything is touched
        for bad in (-1, 201, 1000):
            r = m.do_swappiness_set(bad)
            if "error" not in r or r.get("ok"):
                fails.append(f"out-of-range value {bad} not rejected: {r!r}")
        with open(proc_path) as f:
            if f.read().strip() != "60":
                fails.append("an out-of-range set touched the live value anyway")
        if os.path.exists(m._swappiness_dropin_path()):
            fails.append("an out-of-range set wrote a drop-in anyway")

        # a real set: live value moves, drop-in written, prior ledgered
        r = m.do_swappiness_set(30)
        if not r.get("ok") or r.get("measured") != 30 or r.get("prior") != 60:
            fails.append(f"clean set(30) wrong: {r!r}")
        with open(proc_path) as f:
            if f.read().strip() != "30":
                fails.append("set(30) didn't move the live fixture")
        dropin = m._swappiness_dropin_path()
        if not os.path.exists(dropin):
            fails.append("set(30) didn't write the sysctl.d drop-in")
        else:
            with open(dropin) as f:
                body = f.read()
            if "vm.swappiness = 30" not in body:
                fails.append(f"drop-in body wrong: {body!r}")
            if not dropin.startswith(sysctl_root):
                fails.append(f"drop-in escaped the fixture sysctl root: {dropin!r}")

        # IDEMPOTENT PRIOR: a second, third set must NOT re-ledger — prior
        # stays the TRUE original (60), or reset would restore to the wrong
        # number
        r2 = m.do_swappiness_set(100)
        if r2.get("prior") != 60:
            fails.append(f"prior got re-ledgered on a second set: {r2!r}")
        r3 = m.do_swappiness_set(45)
        if r3.get("prior") != 60:
            fails.append(f"prior got re-ledgered on a third set: {r3!r}")

        # reset: restores the TRUE original, not the last-set value (45)
        r = m.do_swappiness_reset()
        if not r.get("ok") or r.get("restored_to") != 60 or r.get("measured") != 60:
            fails.append(f"reset didn't restore the true original: {r!r}")
        with open(proc_path) as f:
            if f.read().strip() != "60":
                fails.append("reset didn't move the live fixture back to 60")
        if os.path.exists(dropin):
            fails.append("reset left the drop-in in place")
        status_after = m.query_swappiness_status()
        if status_after.get("prior") is not None:
            fails.append(f"reset didn't clear the ledgered prior: {status_after!r}")

    # --- reset with nothing ever ledgered: clean error, not a crash --------
    with tempfile.TemporaryDirectory() as tmp:
        m, _proc_path, _sysctl_root = _fresh_module(tmp)
        r = m.do_swappiness_reset()
        if r.get("ok") or "error" not in r:
            fails.append(f"reset with nothing ledgered should error cleanly: {r!r}")

    # --- THE NEGATIVE CONTROL (ruling 41b72476): the live write silently --
    # doesn't move the value -- a read-only fixture standing in for a
    # write path that isn't actually writable (e.g. a hardening gap).
    # do_swappiness_set must report ok=False and name the disagreement,
    # never trust that "the write call didn't raise" means success.
    with tempfile.TemporaryDirectory() as tmp:
        m, proc_path, _sysctl_root = _fresh_module(tmp)
        os.chmod(proc_path, stat.S_IRUSR)  # read-only: write() will raise
        r = m.do_swappiness_set(10)
        if r.get("ok") or "error" not in r:
            fails.append(
                f"a live value that didn't actually move was reported as ok:"
                f" {r!r} (the exact failure class ruling 41b72476 exists to"
                f" catch — a written drop-in with an unmoved live value)")
        os.chmod(proc_path, stat.S_IRUSR | stat.S_IWUSR)  # tempdir cleanup

    if fails:
        print("SWAPPINESS TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("swappiness ok: status/range-validation/set/idempotent-prior/reset"
          " all correct; honest failure when the live write doesn't move"
          " the world")


if __name__ == "__main__":
    main()
