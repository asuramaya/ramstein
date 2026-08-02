#!/usr/bin/env python3
"""
Tests for ramsteind's oomd swap-enrollment verb (ruling 41b72476: a
layer-3 verb succeeds when the pill's own OBSERVATION changes, never
merely when a file is written). Two things get exercised:

1. _oomd_enroll_preflight()'s conjunction math (mem-used% AND swap-used%
   both over systemd-oomd's own live limit) with a fake get_status() --
   pure arithmetic, no subprocess needed except the limit read.
2. The full enroll/disenroll flow against a stateful fake systemctl +
   oomctl pair: `restart systemd-oomd` re-syncs a marker file to match
   whichever drop-in currently exists (modeling "oomd re-discovers
   reality on restart"), and the fake oomctl reports enrolled/unenrolled
   based on that marker -- so both the SUCCESS path (world moves, verb
   reports ok) and the CORE Fulcrum-standard case (the file changes but
   the world doesn't, verb must report FAILURE honestly, never silent
   success) are exercised against something that actually simulates the
   mechanism rather than a fixed canned answer.

Run as: python3 tests/test_oomd_enroll.py
"""
import atexit
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

# STATE_DIR (and so LEDGER_PATH) is a module-level constant computed once
# at import time from this env var, unlike _systemd_root()'s live re-read
# -- must be set before exec_module, or ledger writes fall through to the
# real, unwritable /var/lib/ramstein.
_LEDGER_FIXTURE_DIR = tempfile.mkdtemp(prefix="ramstein-oomd-enroll-test-")
atexit.register(shutil.rmtree, _LEDGER_FIXTURE_DIR, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _LEDGER_FIXTURE_DIR

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

# Same live-captured/systemd-source-derived shapes as test_oom_coexist.py.
UNENROLLED_DUMP = """Dry Run: no
Swap Used Limit: 90.00%
Default Memory Pressure Limit: 60.00%
Default Memory Pressure Duration: 20s
System Context:
\tMemory: Used: 37.9G, Total: 61.2G
\tSwap: Used: 7.9G, Total: 7.9G
Swap Monitored CGroups:
Memory Pressure Monitored CGroups:
"""

ENROLLED_DUMP = UNENROLLED_DUMP.replace(
    "Swap Monitored CGroups:\n",
    "Swap Monitored CGroups:\n"
    "\tPath: /user.slice/user-1000.slice/user@1000.service\n"
    "\t\tSwap Usage: 4.0K\n",
)

# REAL, LIVE-CAPTURED (2026-08-02, same as test_oom_coexist.py): pressure
# enrolled, swap genuinely empty -- the exact dump that made `ramstein
# oomd status` lie on the operator's own machine (alfred, DM #3250).
LIVE_PRESSURE_ONLY_DUMP = """Dry Run: no
Swap Used Limit: 90.00%
Default Memory Pressure Limit: 60.00%
Default Memory Pressure Duration: 20s
System Context:
\tMemory: Used: 22.9G, Total: 61.2G
\tSwap: Used: 1.1G, Total: 7.9G
Swap Monitored CGroups:
Memory Pressure Monitored CGroups:
\tPath: /user.slice/user-1000.slice/user@1000.service
\t\tMemory Pressure Limit: 50.00%
"""


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def _fake_status(mem_used_pct, swap_used_pct, swap_total_bytes=8 * 1024**3):
    total = 61 * 1024**3
    avail = int(total * (1 - mem_used_pct / 100.0))
    swap_free = int(swap_total_bytes * (1 - swap_used_pct / 100.0)) if swap_total_bytes else 0
    return {"memory": {"total": total, "available": avail,
                        "swap_total": swap_total_bytes, "swap_free": swap_free}}


def test_preflight_math(tmp):
    """Pure arithmetic against a fake get_status(); only needs oomctl on
    PATH for the limit read (_oomd_swap_used_limit_percent)."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{UNENROLLED_DUMP}
FIXTURE_EOF
""")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    fails = []
    try:
        cases = [
            ("no status at all", lambda: {}, "no status yet"),
            ("no swap configured",
             lambda: _fake_status(50, 0, swap_total_bytes=0),
             "no swap configured"),
            ("safe: both well under limit",
             lambda: _fake_status(50, 50), None),
            # THE CONJUNCTION, not an "either": one side over the limit
            # alone must NOT refuse.
            ("mem over, swap under -> still safe",
             lambda: _fake_status(95, 50), None),
            ("swap over, mem under -> still safe",
             lambda: _fake_status(50, 95), None),
            ("BOTH over the limit -> refuse",
             lambda: _fake_status(95, 95), "refusing:"),
        ]
        for name, get_status, expect_substr in cases:
            got = ramsteind._oomd_enroll_preflight(get_status)
            if expect_substr is None:
                if got is not None:
                    fails.append(f"[{name}] expected safe (None), got: {got!r}")
            else:
                if got is None or expect_substr not in got:
                    fails.append(
                        f"[{name}] expected a refusal containing {expect_substr!r}, got: {got!r}")
    finally:
        os.environ["PATH"] = old_path
    return fails


def _fake_systemd_pair(tmp, dropin_path, marker_path, always_unenrolled=False,
                        stuck_dump=None):
    """systemctl: is-active/daemon-reload succeed; `restart systemd-oomd`
    re-syncs marker_path to match whether dropin_path currently exists --
    modeling oomd re-discovering reality on restart, not a one-way flag.
    oomctl reports enrolled iff the marker exists, UNLESS always_
    unenrolled -- simulating a restart that doesn't actually fix anything
    (the world-didn't-move case ruling 41b72476 exists to catch) -- or
    stuck_dump, which pins oomctl to an EXACT fixed dump regardless of
    the marker (for LIVE_PRESSURE_ONLY_DUMP: pressure enrolled from
    something else entirely, swap never budges no matter what this verb
    does -- alfred's DM #3250 real-world case)."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
if [ "$1" = "is-active" ]; then
  [ "$2" = "systemd-oomd" ] && {{ echo active; exit 0; }}
  echo inactive; exit 3
fi
if [ "$1" = "daemon-reload" ]; then
  exit 0
fi
if [ "$1" = "restart" ] && [ "$2" = "systemd-oomd" ]; then
  if [ -f "{dropin_path}" ]; then touch "{marker_path}"; else rm -f "{marker_path}"; fi
  exit 0
fi
exit 1
""")
    if stuck_dump is not None:
        _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{stuck_dump}
FIXTURE_EOF
""")
    elif always_unenrolled:
        _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{UNENROLLED_DUMP}
FIXTURE_EOF
""")
    else:
        _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
if [ -f "{marker_path}" ]; then
cat <<'FIXTURE_EOF'
{ENROLLED_DUMP}
FIXTURE_EOF
else
cat <<'FIXTURE_EOF'
{UNENROLLED_DUMP}
FIXTURE_EOF
fi
""")
    return d


def test_enroll_success(tmp):
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker")
    fakebin = _fake_systemd_pair(tmp, dropin, marker)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50))
        if not result.get("ok"):
            fails.append(f"expected success, got: {result!r}")
        if not os.path.exists(dropin):
            fails.append("drop-in file was not written")
        elif open(dropin).read() != ramsteind._OOMD_ENROLL_DROPIN_BODY:
            fails.append("drop-in content doesn't match the expected body")
        status = ramsteind.query_oomd_status(lambda: _fake_status(50, 50))
        if not status["enrolled"]:
            fails.append(f"query_oomd_status disagrees after a successful enroll: {status!r}")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


def test_preflight_refusal_blocks_the_write(tmp):
    """The dangerous case: mem+swap both critical. Must refuse BEFORE
    writing anything or touching systemd at all."""
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker-refusal")
    fakebin = _fake_systemd_pair(tmp, dropin, marker)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(95, 95))
        if result.get("ok"):
            fails.append(f"expected a refusal, got success: {result!r}")
        if "error" not in result or "refusing:" not in result["error"]:
            fails.append(f"refusal missing the expected message shape: {result!r}")
        if os.path.exists(dropin):
            fails.append("preflight refusal still wrote the drop-in file")
        if os.path.exists(marker):
            fails.append("preflight refusal still triggered a restart (marker touched)")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


def test_honest_failure_when_world_does_not_move(tmp):
    """THE core test for ruling 41b72476: the drop-in gets written and
    systemd-oomd gets restarted successfully, but oomctl still reports
    unenrolled afterward (simulating some other reason enrollment didn't
    actually take). The verb must report FAILURE, not silent success --
    the file changed, the world didn't."""
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker-stuck")
    fakebin = _fake_systemd_pair(tmp, dropin, marker, always_unenrolled=True)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50))
        if result.get("ok"):
            fails.append(f"expected a reported failure (world didn't move), got: {result!r}")
        if not os.path.exists(dropin):
            fails.append("the drop-in should still have been written even though enrollment"
                          " didn't take -- this test only means anything if the write happened")
        if "error" not in result or "the world didn't" not in result["error"]:
            fails.append(f"failure message doesn't name the write/measure disagreement: {result!r}")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


def test_honest_failure_with_pressure_already_enrolled(tmp):
    """THE REAL-WORLD CASE (alfred, DM #3250, found live on the operator's
    own machine after verb #1 shipped): memory-pressure already enrolled
    from something else entirely, swap never budges no matter what this
    verb does. A prior version's re-verify used a predicate that scooped
    BOTH sections once anchored past "Swap Monitored CGroups:", so the
    pressure entry alone satisfied it and the verb reported success on a
    machine with zero actual swap protection -- the exact failure this
    whole night's work exists to catch, in the tool built to catch it.
    Must report FAILURE, not be fooled by an unrelated section."""
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker-pressure-only")
    fakebin = _fake_systemd_pair(tmp, dropin, marker,
                                  stuck_dump=LIVE_PRESSURE_ONLY_DUMP)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50))
        if result.get("ok"):
            fails.append(
                f"pressure being enrolled fooled the swap-specific re-verify"
                f" into reporting success: {result!r}")
        status = ramsteind.query_oomd_status(lambda: _fake_status(50, 50))
        if status["enrolled"]:
            fails.append(f"query_oomd_status also fooled by the pressure"
                          f" section: {status!r}")
        # the broad coexistence question is a SEPARATE, correct concern --
        # pressure alone is a real backstop, just not this verb's backstop
        if ramsteind._coexisting_oom_fighter() != "systemd-oomd":
            fails.append("pressure-only enrollment should still count for"
                          " the broad coexistence question")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


def test_disenroll(tmp):
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker-disenroll")
    fakebin = _fake_systemd_pair(tmp, dropin, marker)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        enrolled = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50))
        if not enrolled.get("ok"):
            fails.append(f"setup: enroll should have succeeded, got: {enrolled!r}")
            return fails
        result = ramsteind.do_oomd_disenroll()
        if not result.get("ok"):
            fails.append(f"expected disenroll success, got: {result!r}")
        if os.path.exists(dropin):
            fails.append("drop-in still present after disenroll")
        status = ramsteind.query_oomd_status(lambda: _fake_status(50, 50))
        if status["enrolled"]:
            fails.append(f"still reports enrolled after disenroll: {status!r}")
        if status["ramstein_dropin_present"]:
            fails.append(f"still reports the drop-in present after disenroll: {status!r}")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


def main():
    all_fails = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, fn in [
            ("preflight math", test_preflight_math),
            ("enroll success", test_enroll_success),
            ("preflight refusal blocks the write", test_preflight_refusal_blocks_the_write),
            ("honest failure when the world doesn't move", test_honest_failure_when_world_does_not_move),
            ("honest failure with pressure already enrolled (real-world case)",
             test_honest_failure_with_pressure_already_enrolled),
            ("disenroll", test_disenroll),
        ]:
            fails = fn(tmp)
            if fails:
                all_fails.append((name, fails))
            else:
                print(f"oomd enroll: {name} ok")

    if all_fails:
        print("OOMD ENROLL TEST FAILED:")
        for name, fails in all_fails:
            for f in fails:
                print(f"  - [{name}] {f}")
        sys.exit(1)
    print("oomd enroll ok: preflight conjunction, enroll/disenroll, and the"
          " honest-failure-on-a-world-that-didn't-move case all correct")


if __name__ == "__main__":
    main()
