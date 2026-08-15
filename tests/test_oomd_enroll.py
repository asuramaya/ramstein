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

SESSION_CGROUP_PATH = "/user.slice/user-1000.slice/user@1000.service"

ENROLLED_DUMP = UNENROLLED_DUMP.replace(
    "Swap Monitored CGroups:\n",
    "Swap Monitored CGroups:\n"
    f"\tPath: {SESSION_CGROUP_PATH}\n"
    "\t\tSwap Usage: 4.0K\n",
)

# THE PRECISION FIX'S OWN CASE (thread 5607ab3c): a cgroup IS swap-
# enrolled, but it's a leftover from before oomd's last restart -- some
# OTHER user's session, never this one. _oomd_any_swap_cgroup_enrolled (aggregate)
# reads this as enrolled=true; _oomd_session_swap_effective must not.
OTHER_SESSION_ENROLLED_DUMP = UNENROLLED_DUMP.replace(
    "Swap Monitored CGroups:\n",
    "Swap Monitored CGroups:\n"
    "\tPath: /user.slice/user-999.slice/user@999.service\n"
    "\t\tSwap Usage: 4.0K\n",
)

# cfg only ever needs owner_uid for anything in this file.
_FAKE_CFG = {"owner_uid": 1000}

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
if [ "$1" = "show" ] && [ "$4" = "user@1000.service" ]; then
  echo "ControlGroup={SESSION_CGROUP_PATH}"
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
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG, dry_run=False)
        if not result.get("ok"):
            fails.append(f"expected success, got: {result!r}")
        if not os.path.exists(dropin):
            fails.append("drop-in file was not written")
        elif open(dropin).read() != ramsteind._OOMD_ENROLL_DROPIN_BODY:
            fails.append("drop-in content doesn't match the expected body")
        status = ramsteind.query_oomd_status(lambda: _fake_status(50, 50), _FAKE_CFG)
        if not status["enrolled"]:
            fails.append(f"query_oomd_status disagrees after a successful enroll: {status!r}")
        if status["effective"] is not True:
            fails.append(f"session cgroup is genuinely enrolled -- effective should be"
                          f" True, not {status['effective']!r}")
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
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(95, 95), _FAKE_CFG, dry_run=False)
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
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG, dry_run=False)
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
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG, dry_run=False)
        if result.get("ok"):
            fails.append(
                f"pressure being enrolled fooled the swap-specific re-verify"
                f" into reporting success: {result!r}")
        status = ramsteind.query_oomd_status(lambda: _fake_status(50, 50), _FAKE_CFG)
        if status["enrolled"]:
            fails.append(f"query_oomd_status also fooled by the pressure"
                          f" section: {status!r}")
        if status["effective"] is not False:
            fails.append(f"swap section parsed cleanly and is genuinely empty --"
                          f" effective should be False, not {status['effective']!r}")
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


def test_enroll_fooled_by_other_session_enrollment(tmp):
    """THE PUSH-BACK CASE (alfred, DM 4527): _oomd_apply_enrollment used
    to re-verify with the AGGREGATE predicate (_oomd_any_swap_cgroup_enrolled --
    "is ANY cgroup swap-enrolled"), the same aggregate/instance collapse
    thread 5607ab3c fixed for the status read, just sitting in a more
    consequential place -- an acceptance test whose entire job is
    telling "my act worked" from "the state was already true". A
    machine where some OTHER session's cgroup is already swap-enrolled
    (a leftover from before oomd's last restart, modeled here by
    OTHER_SESSION_ENROLLED_DUMP staying stuck regardless of what this
    verb writes) must NOT pass this verb's acceptance test just because
    the aggregate reads true. Same shape as the pressure-only case
    above, one layer more specific: not a different SECTION fooling the
    check, but a different CGROUP inside the right section."""
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker-other-session")
    fakebin = _fake_systemd_pair(tmp, dropin, marker,
                                  stuck_dump=OTHER_SESSION_ENROLLED_DUMP)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG, dry_run=False)
        if result.get("ok"):
            fails.append(
                f"someone else's cgroup being enrolled fooled the"
                f" session-specific re-verify into reporting success: {result!r}")
        if result.get("indeterminate"):
            fails.append(f"this is a CONFIRMED false, not an indeterminate --"
                          f" oomctl and systemctl both answered cleanly: {result!r}")
        if "error" not in result or "the world didn't" not in result["error"]:
            fails.append(f"failure message doesn't name the write/measure"
                          f" disagreement: {result!r}")
        # confirms the fixture models the real bug, not a strawman: the
        # OLD aggregate predicate genuinely would have been fooled here
        if not ramsteind._oomd_any_swap_cgroup_enrolled():
            fails.append("fixture is wrong: the aggregate check should"
                          " read enrolled=true for this dump")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


def test_enroll_indeterminate_when_effectiveness_cannot_be_confirmed(tmp):
    """Alfred's second option, taken where it applies: when the
    precision predicate itself can't answer (here: the session cgroup's
    systemctl lookup fails, right after an otherwise-successful write +
    restart), the verb must report ok=False WITH indeterminate=True --
    not a confirmed failure (the world might be fine, we just can't see
    it) and never ok=True (ruling 41b72476: an unconfirmed observation
    is not a confirmed change)."""
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "systemctl"), """#!/usr/bin/env bash
if [ "$1" = "is-active" ]; then echo active; exit 0; fi
if [ "$1" = "daemon-reload" ]; then exit 0; fi
if [ "$1" = "restart" ]; then exit 0; fi
exit 1
""")
    _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{ENROLLED_DUMP}
FIXTURE_EOF
""")

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = d + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG, dry_run=False)
        if result.get("ok"):
            fails.append(f"expected ok=False on an unconfirmable write, got: {result!r}")
        if not result.get("indeterminate"):
            fails.append(f"expected indeterminate=True (couldn't confirm either"
                          f" direction, not a confirmed failure): {result!r}")
        if result.get("measured_effective") is not None:
            fails.append(f"measured_effective should be None when unconfirmable:"
                          f" {result!r}")
        if not os.path.exists(dropin):
            fails.append("the drop-in should still have been written -- the"
                          " write itself succeeded, only verification didn't")
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
        enrolled = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG, dry_run=False)
        if not enrolled.get("ok"):
            fails.append(f"setup: enroll should have succeeded, got: {enrolled!r}")
            return fails
        result = ramsteind.do_oomd_disenroll(_FAKE_CFG)
        if not result.get("ok"):
            fails.append(f"expected disenroll success, got: {result!r}")
        if os.path.exists(dropin):
            fails.append("drop-in still present after disenroll")
        status = ramsteind.query_oomd_status(lambda: _fake_status(50, 50), _FAKE_CFG)
        if status["enrolled"]:
            fails.append(f"still reports enrolled after disenroll: {status!r}")
        if status["ramstein_dropin_present"]:
            fails.append(f"still reports the drop-in present after disenroll: {status!r}")
        if status["effective"] is not False:
            fails.append(f"swap section empty after disenroll -- effective should be"
                          f" False, not {status['effective']!r}")
    finally:
        os.environ["PATH"] = old_path
        if old_root is None:
            os.environ.pop("RAMSTEIN_SYSTEMD_ROOT", None)
        else:
            os.environ["RAMSTEIN_SYSTEMD_ROOT"] = old_root
    return fails


# --- the precision fix itself (thread 5607ab3c): _oomd_session_swap_
# effective's three states, tested directly rather than only through the
# enroll/disenroll flow above. Alfred's own instruction (DM 4506): "I'd
# rather see the unknown branch tested than the happy path" -- so the
# unknown cases below outnumber the resolved ones on purpose.

def test_effective_true_for_own_session(tmp):
    """The precision case working as intended: the session's own cgroup
    IS in oomctl's swap-enrolled list."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{ENROLLED_DUMP}
FIXTURE_EOF
""")
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
echo "ControlGroup={SESSION_CGROUP_PATH}"
exit 0
""")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    fails = []
    try:
        got = ramsteind._oomd_session_swap_effective(_FAKE_CFG)
        if got is not True:
            fails.append(f"expected True, got: {got!r}")
    finally:
        os.environ["PATH"] = old_path
    return fails


def test_effective_false_for_someone_elses_leftover_cgroup(tmp):
    """THE BUG THIS FIX EXISTS TO CLOSE (the fit report, DM 4426):
    _oomd_any_swap_cgroup_enrolled (aggregate) reads OTHER_SESSION_ENROLLED_DUMP as
    enrolled=true -- SOME cgroup is swap-monitored. But it isn't THIS
    session's own cgroup (a leftover enrollment from before oomd's last
    restart, or someone else's). The precise check must say False, not
    be fooled the same way the aggregate one always was."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{OTHER_SESSION_ENROLLED_DUMP}
FIXTURE_EOF
""")
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
echo "ControlGroup={SESSION_CGROUP_PATH}"
exit 0
""")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    fails = []
    try:
        got = ramsteind._oomd_session_swap_effective(_FAKE_CFG)
        if got is not False:
            fails.append(f"expected False (a DIFFERENT cgroup is enrolled,"
                          f" not this session's own), got: {got!r}")
        # the aggregate question genuinely IS true here -- confirms the
        # fixture models the real bug, not a strawman
        if not ramsteind._oomd_any_swap_cgroup_enrolled():
            fails.append("fixture is wrong: the aggregate check should"
                          " read enrolled=true for this dump")
    finally:
        os.environ["PATH"] = old_path
    return fails


def test_effective_unknown_when_oomctl_missing(tmp):
    """oomctl not on PATH at all -- OSError, not a parse failure. Must
    be None, never silently False."""
    d = tempfile.mkdtemp(dir=tmp)  # empty -- no oomctl, no systemctl
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d
    fails = []
    try:
        got = ramsteind._oomd_session_swap_effective(_FAKE_CFG)
        if got is not None:
            fails.append(f"expected None (oomctl unreachable), got: {got!r}")
    finally:
        os.environ["PATH"] = old_path
    return fails


def test_effective_unknown_when_oomctl_exits_nonzero(tmp):
    """oomctl runs but fails -- a real, distinct failure mode from
    "not found at all"; both must land on None."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "oomctl"), "#!/usr/bin/env bash\nexit 1\n")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    fails = []
    try:
        got = ramsteind._oomd_session_swap_effective(_FAKE_CFG)
        if got is not None:
            fails.append(f"expected None (oomctl exited nonzero), got: {got!r}")
    finally:
        os.environ["PATH"] = old_path
    return fails


def test_effective_unknown_when_dump_unparseable(tmp):
    """oomctl succeeds but its output doesn't carry a Swap Monitored
    CGroups header at all -- an output shape this parser doesn't
    recognize is NOT the same fact as "recognized it, zero entries"."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "oomctl"), """#!/usr/bin/env bash
echo "unexpected future oomctl output format, no known headers at all"
""")
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
echo "ControlGroup={SESSION_CGROUP_PATH}"
exit 0
""")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    fails = []
    try:
        got = ramsteind._oomd_session_swap_effective(_FAKE_CFG)
        if got is not None:
            fails.append(f"expected None (unrecognized oomctl output shape),"
                          f" got: {got!r}")
    finally:
        os.environ["PATH"] = old_path
    return fails


def test_effective_unknown_when_session_cgroup_unresolvable(tmp):
    """oomctl parses cleanly, but systemctl can't resolve user@.service's
    own cgroup (unit doesn't exist, systemctl itself fails) -- the
    daemon can't ask "is MINE in the list" without knowing what "mine"
    is, so this must be None too, not a guess in either direction."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{ENROLLED_DUMP}
FIXTURE_EOF
""")
    _write_exec(os.path.join(d, "systemctl"), "#!/usr/bin/env bash\nexit 1\n")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    fails = []
    try:
        got = ramsteind._oomd_session_swap_effective(_FAKE_CFG)
        if got is not None:
            fails.append(f"expected None (session cgroup unresolvable),"
                          f" got: {got!r}")
    finally:
        os.environ["PATH"] = old_path
    return fails


def test_default_is_dry_run(tmp):
    """Requirement 1 of alfred's ratification (msg 3429): the SAFE default
    is load-bearing, not a formality -- a caller that omits dry_run (a
    pill's own runRamsteinJson call before this session's CLI fix lands,
    or any malformed/truncated request) must get a PREVIEW, never a real
    write. Verified at the do_oomd_enroll layer, which is what the
    dispatch table's own `req.get("dry_run", True)` ultimately calls."""
    fails = []
    systemd_root = tempfile.mkdtemp(dir=tmp)
    dropin = os.path.join(systemd_root, ramsteind._OOMD_ENROLL_DROPIN_REL)
    marker = os.path.join(tmp, "oomd-enrolled-marker-default")
    fakebin = _fake_systemd_pair(tmp, dropin, marker)

    old_path, old_root = os.environ.get("PATH", ""), os.environ.get("RAMSTEIN_SYSTEMD_ROOT")
    os.environ["PATH"] = fakebin + os.pathsep + old_path
    os.environ["RAMSTEIN_SYSTEMD_ROOT"] = systemd_root
    try:
        result = ramsteind.do_oomd_enroll(lambda: _fake_status(50, 50), _FAKE_CFG)  # no dry_run arg
        if not result.get("dry_run"):
            fails.append(f"omitting dry_run did not default to a preview: {result!r}")
        if "would_write" not in result or "note" not in result:
            fails.append(f"preview missing expected fields: {result!r}")
        if os.path.exists(dropin):
            fails.append("the default (no dry_run arg) call wrote the drop-in anyway")
        if os.path.exists(marker):
            fails.append("the default (no dry_run arg) call restarted systemd-oomd anyway")
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
            ("enroll fooled by someone else's session enrollment (push-back case)",
             test_enroll_fooled_by_other_session_enrollment),
            ("enroll indeterminate when effectiveness can't be confirmed",
             test_enroll_indeterminate_when_effectiveness_cannot_be_confirmed),
            ("disenroll", test_disenroll),
            ("default (no dry_run arg) is a safe preview", test_default_is_dry_run),
            ("effective: true for own session", test_effective_true_for_own_session),
            ("effective: false for someone else's leftover cgroup",
             test_effective_false_for_someone_elses_leftover_cgroup),
            ("effective: unknown when oomctl is missing", test_effective_unknown_when_oomctl_missing),
            ("effective: unknown when oomctl exits nonzero",
             test_effective_unknown_when_oomctl_exits_nonzero),
            ("effective: unknown when the dump is unparseable",
             test_effective_unknown_when_dump_unparseable),
            ("effective: unknown when the session cgroup is unresolvable",
             test_effective_unknown_when_session_cgroup_unresolvable),
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
    print("oomd enroll ok: preflight conjunction, enroll/disenroll, the"
          " honest-failure-on-a-world-that-didn't-move case, and the"
          " session-precision fix's true/false/unknown states all correct")


if __name__ == "__main__":
    main()
