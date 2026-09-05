#!/usr/bin/env python3
"""
Tests V4's memory stance (operator ruling, relayed msg 7110/7117/7119/
7123/7127, 2026-09-05): ramstein stops being a pure gauge and starts
keeping a short, operator-authored policy applied as cgroup memory
controls -- protect (memory.low up the whole ancestor chain, measurement
(c), decision e39cd759), expendable (memory.high only while hot, released
when calm), cap (a standing memory.high), a classifier (leaf-only
matching, unit_glob/comm/exe_glob, first-match-wins, unclassified never
capped), `ramstein ledger` (memory by thing, unclassified grouped by
exe/comm), and `ramstein stance rollback` (every touched cgroup back to
its off value, read back to confirm).

Covers:
  - _load_stance: missing file -> zero rules, no error; malformed JSON ->
    error; an invalid rule -> error naming which one; a load failure must
    never apply PART of a stance.
  - _match_leaf: unit_glob/comm/exe_glob, leaf-only (a v1 seed bug matched
    a PARENT of every app scope), first-match-wins, no match ->
    unclassified.
  - _leaf_road / _ancestor_chain_for_protect: the delegation boundary
    (measurement (a), decision 93b9b9e1) and the full ancestor chain
    (measurement (c), decision e39cd759) -- bridge for anything still
    inside user@<uid>.service's own subtree, root for user@<uid>.service
    and user-<uid>.slice themselves and for anything outside a session at
    all (system.slice/docker).
  - _apply_protect_tier: floor sized to the sum of protect-tier RESIDENT
    bytes (item 5, alfred msg 7436 -- charged/cache-inclusive usage never
    inflates the floor), capped at stance_protect_floor_ceiling_pct of
    MemTotal, the ledger-visible "pinned" flag when a leak would
    otherwise ratchet it past that ceiling, and the floor's own basis
    string naming the session count.
  - do_stance_rollback: resets every touched entry via the road it was
    written with, a vanished unit counts as a pass, a failure stays in
    the touched list for the next attempt.
  - apply_stance: a stance file that fails to load rolls back and never
    applies anything; a zero-rule stance is a clean no-op.
  - query_ledger: same-tier instances collapse to one row; unclassified
    groups by exe (falling back to comm) instead of fragmenting into one
    row per pid.

Run as: python3 tests/test_stance.py
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

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-stance-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)


def _reset(tmp):
    ramsteind.STATE_DIR = tmp
    ramsteind.STANCE_TOUCHED_PATH = os.path.join(tmp, "stance_touched.json")


def _stance_file(tmp, doc_or_text):
    path = os.path.join(tmp, "stance.json")
    with open(path, "w") as f:
        if isinstance(doc_or_text, str):
            f.write(doc_or_text)
        else:
            json.dump(doc_or_text, f)
    os.environ["RAMSTEIN_STANCE_PATH"] = path
    return path


def _proc(pid, comm, rss=0, swap=0):
    return {"pid": pid, "comm": comm, "rss": rss, "swap": swap,
            "state": "S", "ppid": 1, "starttime": 0}


# --- _load_stance -------------------------------------------------------

def test_load_stance_missing_and_malformed(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "does-not-exist.json")
    rules, err = ramsteind._load_stance()
    if rules != [] or err is not None:
        fails.append(f"missing stance file should be zero rules, no error: {rules!r} {err!r}")

    _stance_file(tmp, "{not json")
    rules, err = ramsteind._load_stance()
    if rules is not None or not err:
        fails.append("malformed JSON should error, apply nothing")

    _stance_file(tmp, {"rules": [{"match": {"comm": "chrome"}, "tier": "bogus"}]})
    rules, err = ramsteind._load_stance()
    if rules is not None or "rule 0" not in (err or ""):
        fails.append(f"an invalid tier should error naming the rule index, got {err!r}")

    _stance_file(tmp, {"rules": [{"match": {"comm": "postgres"}, "tier": "protect"}]})
    rules, err = ramsteind._load_stance()
    if err is not None or len(rules) != 1:
        fails.append(f"a valid single-rule stance should load cleanly: {rules!r} {err!r}")

    _stance_file(tmp, {"rules": [{"match": {"comm": "x"}, "tier": "cap"}]})
    rules, err = ramsteind._load_stance()
    if rules is not None or "memory_high" not in (err or ""):
        fails.append("a cap rule missing a size must error")


# --- _match_leaf ----------------------------------------------------------

def test_match_leaf_unit_comm_exe_glob(fails):
    rules = [
        {"match": {"unit_glob": "app-*.google.Chrome-*.scope"}, "tier": "expendable"},
        {"match": {"comm": "postgres"}, "tier": "protect"},
        {"match": {"exe_glob": "/opt/claude/versions/*"}, "tier": "protect"},
    ]
    orig_exe = ramsteind._proc_exe
    ramsteind._proc_exe = lambda pid: {101: "/opt/claude/versions/1.2.3/claude"}.get(pid)
    try:
        idx, rule = ramsteind._match_leaf(
            "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
            "/app.slice/app-com.google.Chrome-555.scope",
            [_proc(555, "chrome")], rules)
        if idx != 0:
            fails.append(f"unit_glob should match Chrome's own scope, got {idx}")

        idx, rule = ramsteind._match_leaf("/sys/fs/cgroup/x.scope",
                                          [_proc(9, "postgres")], rules)
        if idx != 1:
            fails.append(f"comm should match postgres, got {idx}")

        idx, rule = ramsteind._match_leaf("/sys/fs/cgroup/y.scope",
                                          [_proc(101, "1.2.3")], rules)
        if idx != 2:
            fails.append(f"exe_glob should match the fleet's version-string comm"
                         f" via /proc/pid/exe, got {idx}")

        idx, rule = ramsteind._match_leaf("/sys/fs/cgroup/z.scope",
                                          [_proc(9, "nginx")], rules)
        if idx is not None:
            fails.append(f"no rule should match nginx -- expected unclassified, got {idx}")
    finally:
        ramsteind._proc_exe = orig_exe


def test_v1_parent_scope_bug_does_not_match_a_leaf(fails):
    # v1's own seed bug (alfred msg 7119 note 3): user@*.service is the
    # PARENT of every app scope, not a leaf -- must never accidentally
    # match Chrome's own leaf scope.
    rules = [{"match": {"unit_glob": "user@*.service"}, "tier": "protect"}]
    idx, _ = ramsteind._match_leaf(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
        "/app.slice/app-com.google.Chrome-555.scope",
        [_proc(555, "chrome")], rules)
    if idx is not None:
        fails.append("a parent-scope unit_glob must not match a leaf's own basename")


def test_unit_glob_wins_regardless_of_rule_order(fails):
    # alfred msg 7409/7427 item 4: a scope's own unit_glob must beat any
    # per-pid exe_glob match even when the exe_glob rule is listed
    # FIRST in the file -- classification is about what the scope IS,
    # not about rule position.
    rules = [
        {"match": {"exe_glob": "/usr/bin/protected-thing"}, "tier": "protect"},
        {"match": {"unit_glob": "expendable-*.scope"}, "tier": "expendable"},
    ]
    orig_exe = ramsteind._proc_exe
    ramsteind._proc_exe = lambda pid: "/usr/bin/protected-thing"
    try:
        idx, rule = ramsteind._match_leaf(
            "/sys/fs/cgroup/expendable-123.scope", [_proc(1, "thing")], rules)
    finally:
        ramsteind._proc_exe = orig_exe
    if rule is None or rule["tier"] != "expendable":
        fails.append(f"unit_glob must win over an earlier exe_glob rule: {rule}")


def test_exe_match_uses_dominant_proc_only(fails):
    # alfred msg 7409/7427 item 4: a minority co-resident process must
    # never hijack classification -- only the DOMINANT process (by
    # resident bytes) is checked against comm/exe_glob rules.
    rules = [{"match": {"comm": "tiny-outlier"}, "tier": "protect"}]
    idx, rule = ramsteind._match_leaf(
        "/sys/fs/cgroup/mixed.scope",
        [_proc(1, "big-thing", rss=10 * 1024**3),
         _proc(2, "tiny-outlier", rss=1024)],
        rules)
    if rule is not None:
        fails.append(f"a 1KB outlier process must not classify a scope"
                     f" dominated by a 10GB process: {rule}")


def test_claude_in_chrome_shared_scope_resolves_expendable(fails):
    # A live, real finding from `ramstein stance plan` against this very
    # box (alfred msg 7402/7409/7427: "keep that case as a test
    # fixture"): a claude-in-chrome-driven Chrome instance can end up
    # co-resident in the SAME cgroup scope as a protected fleet session
    # (the browser was launched as a child of the claude process,
    # inheriting its cgroup rather than getting its own app-*.scope). A
    # cgroup is the unit of enforcement -- one scope cannot carry two
    # different policies for two different processes inside it.
    #
    # CORRECTED (msg 7409/7427 item 4): the first cut of this fixture
    # asserted the OPPOSITE outcome (protect), reasoning that erring
    # toward "never squeeze" was the safe default -- alfred's live
    # install caught this as an actual bug: it made the plan write
    # MemoryLow=1.4G on the operator's own Chrome, the opposite of the
    # stance. The scope's own systemd identity (its unit name IS a
    # Chrome app scope, and its resident memory is overwhelmingly
    # Chrome's own) is what the classifier must honor -- unit_glob
    # naming the scope itself now wins over any per-pid exe_glob match,
    # checked across ALL rules before any comm/exe match is even
    # considered, regardless of which tier is listed first in the file.
    rules = [
        {"match": {"exe_glob": "~/.local/share/claude/versions/*"}, "tier": "protect"},
        {"match": {"unit_glob": "app-*.google.Chrome-*.scope"}, "tier": "expendable"},
    ]
    # _expand_home_glob is mocked directly (not pwd.getpwuid) so this
    # test doesn't depend on uid 1000's real home directory, which
    # differs by machine (this dev box vs a CI runner vs anywhere else)
    # -- exactly the class of environment-dependent test bug this
    # rewrite fixes, caught live when this fixture passed here but
    # failed on CI the first time it shipped.
    orig_exe = ramsteind._proc_exe
    orig_uid = ramsteind._proc_uid
    orig_expand = ramsteind._expand_home_glob
    ramsteind._proc_exe = lambda pid: {
        1120188: "/opt/google/chrome/chrome",
        1128577: "/home/FAKEUSER/.local/share/claude/versions/2.1.260",
        1129420: "/opt/google/chrome/chrome",
    }.get(pid)
    ramsteind._proc_uid = lambda pid: 1000
    ramsteind._expand_home_glob = lambda pattern, uid: (
        pattern.replace("~", "/home/FAKEUSER", 1) if pattern.startswith("~/") else pattern)
    try:
        idx, rule = ramsteind._match_leaf(
            "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
            "/app.slice/app-com.google.Chrome-1120188.scope",
            [_proc(1120188, "chrome"), _proc(1128577, "claude"),
             _proc(1129420, "chrome")],
            rules)
    finally:
        ramsteind._proc_exe = orig_exe
        ramsteind._proc_uid = orig_uid
        ramsteind._expand_home_glob = orig_expand
    if rule is None or rule["tier"] != "expendable":
        fails.append(f"a scope whose own unit_glob names it (Chrome's app"
                     f" scope) must classify as what it IS, regardless of"
                     f" an incidental co-resident process or rule order,"
                     f" got {rule}")


# --- _leaf_road / _ancestor_chain_for_protect ------------------------------

def test_leaf_road_delegation_boundary(fails):
    road, uid = ramsteind._leaf_road(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
        "/app.slice/app-com.google.Chrome-555.scope")
    if road != "bridge" or uid != 1000:
        fails.append(f"a leaf inside user@1000.service's subtree needs the bridge, got {road}/{uid}")

    road, uid = ramsteind._leaf_road("/sys/fs/cgroup/system.slice/docker-abc123.scope")
    if road != "root" or uid is not None:
        fails.append(f"system.slice/docker leaf should be plain root, got {road}/{uid}")

    # user@1000.service itself is NOT inside its own delegated subtree.
    road, uid = ramsteind._leaf_road(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service")
    if road != "root":
        fails.append(f"user@1000.service itself is system-owned, expected root, got {road}")


def test_ancestor_chain_for_protect(fails):
    chain = ramsteind._ancestor_chain_for_protect(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
        "/app.slice/app-com.google.Chrome-555.scope", 1000)
    roads = {os.path.basename(p): r for p, r in chain}
    expect_bridge = {"app-com.google.Chrome-555.scope", "app.slice"}
    expect_root = {"user@1000.service", "user-1000.slice"}
    got_bridge = {b for b, r in roads.items() if r == "bridge"}
    got_root = {b for b, r in roads.items() if r == "root"}
    if got_bridge != expect_bridge:
        fails.append(f"bridge-road ancestors wrong: {got_bridge} != {expect_bridge}")
    if got_root != expect_root:
        fails.append(f"root-road ancestors wrong: {got_root} != {expect_root}")
    if "user.slice" in roads:
        fails.append("must never touch the top-level user.slice (shared by every user)")

    chain2 = ramsteind._ancestor_chain_for_protect(
        "/sys/fs/cgroup/system.slice/docker-abc.scope", None)
    if chain2 != [("/sys/fs/cgroup/system.slice/docker-abc.scope", "root")]:
        fails.append(f"a non-session leaf should get itself only, root road: {chain2}")


# --- _apply_protect_tier: floor + ceiling ----------------------------------

def test_protect_floor_ceiling(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    cfg = dict(ramsteind.DEFAULTS)
    orig_set = ramsteind._systemctl_set_property
    orig_memtotal = ramsteind._mem_total_bytes
    orig_ancestors = ramsteind._ancestor_chain_for_protect
    calls = []
    ramsteind._systemctl_set_property = lambda unit, prop, val, uid=None: (
        calls.append((unit, prop, val, uid)) or (True, None))
    ramsteind._mem_total_bytes = lambda: 10 * 1024**3  # 10G total
    ramsteind._ancestor_chain_for_protect = lambda path, uid: [(path, "root")]
    try:
        # under ceiling: floor tracks real RESIDENT usage.
        leaves = [{"path": "/a", "unit": "a", "resident": 1 * 1024**3, "road": "root", "uid": None},
                  {"path": "/b", "unit": "b", "resident": 2 * 1024**3, "road": "root", "uid": None}]
        touched = []
        result = ramsteind._apply_protect_tier(cfg, leaves, touched)
        if result["pinned_at_ceiling"]:
            fails.append("3G of 10G (30%, under the 50% default ceiling) should not be pinned")
        if result["floor_bytes"] != 3 * 1024**3:
            fails.append(f"floor should track real usage under the ceiling: {result['floor_bytes']}")
        if result["basis"] != "resident of 2 sessions":
            fails.append(f"floor basis should name the session count: {result['basis']!r}")

        # over ceiling (a "leak"): floor pins at 50% of MemTotal, not the
        # unbounded sum -- alfred msg 7125 note 1.
        calls.clear()
        leaves = [{"path": "/a", "unit": "a", "resident": 8 * 1024**3, "road": "root", "uid": None}]
        touched = []
        result = ramsteind._apply_protect_tier(cfg, leaves, touched)
        if not result["pinned_at_ceiling"]:
            fails.append("8G of 10G should exceed the 50% ceiling and pin")
        if result["floor_bytes"] != 5 * 1024**3:
            fails.append(f"pinned floor should be exactly 50% of MemTotal: {result['floor_bytes']}")

        # item 5 (alfred msg 7436): floor sizes on RESIDENT, not CHARGED --
        # a scope sitting on a pile of reclaimable file cache must not
        # inflate the floor just because memory.current is high.
        calls.clear()
        leaves = [{"path": "/a", "unit": "a", "resident": 1 * 1024**3,
                   "usage": 9 * 1024**3, "road": "root", "uid": None}]
        touched = []
        result = ramsteind._apply_protect_tier(cfg, leaves, touched)
        if result["floor_bytes"] != 1 * 1024**3:
            fails.append("floor must size on resident (1G), not charged (9G) of cache: "
                         f"{result['floor_bytes']}")
        if result["pinned_at_ceiling"]:
            fails.append("1G resident of 10G should not pin, even though charged usage is 9G")
    finally:
        ramsteind._systemctl_set_property = orig_set
        ramsteind._mem_total_bytes = orig_memtotal
        ramsteind._ancestor_chain_for_protect = orig_ancestors


# --- do_stance_rollback -----------------------------------------------------

def test_rollback_resets_and_tracks_failures(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    orig_set = ramsteind._systemctl_set_property
    orig_show = ramsteind._systemctl_show_property
    ramsteind._save_stance_touched([
        {"unit": "good.scope", "prop": "MemoryLow", "road": "root", "uid": None},
        {"unit": "gone.scope", "prop": "MemoryHigh", "road": "root", "uid": None},
        {"unit": "stuck.scope", "prop": "MemoryLow", "road": "root", "uid": None},
    ])

    def fake_set(unit, prop, val, uid=None):
        if unit == "gone.scope":
            return False, "unit not loaded"
        if unit == "stuck.scope":
            return False, "permission denied"
        return True, None

    def fake_show(unit, prop, uid=None):
        if unit == "gone.scope":
            return None  # vanished -- counts as a pass
        if unit == "stuck.scope":
            return "500000000"  # still live, still holding its old value
        return "0"

    ramsteind._systemctl_set_property = fake_set
    ramsteind._systemctl_show_property = fake_show
    try:
        result = ramsteind.do_stance_rollback()
    finally:
        ramsteind._systemctl_set_property = orig_set
        ramsteind._systemctl_show_property = orig_show

    by_unit = {r["unit"]: r for r in result["results"]}
    if not by_unit["good.scope"]["ok"]:
        fails.append("a clean reset should pass")
    if not by_unit["gone.scope"]["ok"]:
        fails.append("a vanished unit should count as a pass, not a failure")
    if by_unit["stuck.scope"]["ok"]:
        fails.append("a set-property failure on a still-live unit should be reported as a failure")
    remaining = ramsteind._load_stance_touched()
    remaining_units = {t["unit"] for t in remaining}
    if "good.scope" in remaining_units or "gone.scope" in remaining_units:
        fails.append(f"resolved entries should not remain touched: {remaining}")
    if "stuck.scope" not in remaining_units:
        fails.append("a genuinely failed reset must stay in the touched list for next time")
    if result["remaining"] != 1:
        fails.append(f"exactly one entry (stuck.scope) should remain: {result['remaining']}")


def test_rollback_refuses_loudly_when_touched_list_unreadable(fails):
    # alfred msg 7409/7427 item 2, found live: rollback against an
    # unreadable (not merely missing) touched-cgroups list reported
    # "complete -- 0 entries" instead of refusing. A genuinely MISSING
    # file is honest zero; anything else unreadable must refuse rather
    # than claim completeness over an unknown number of real entries.
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    # missing file: honest empty, real success.
    result = ramsteind.do_stance_rollback()
    if not result["ok"] or result.get("error"):
        fails.append(f"a genuinely missing touched-list file should be an honest,"
                     f" real success, not a refusal: {result}")

    # unreadable (simulated via a corrupt file, not a permission trick --
    # portable across whatever this test runs as): must refuse loudly.
    with open(ramsteind.STANCE_TOUCHED_PATH, "w") as f:
        f.write("{not valid json")
    result2 = ramsteind.do_stance_rollback()
    if result2["ok"] or not result2.get("error") or result2.get("remaining") is not None:
        fails.append(f"an unreadable (corrupt) touched-list must refuse loudly,"
                     f" not report success: {result2}")


def test_rollback_memory_high_uses_systemd_infinity_not_cgroupfs_max(fails):
    # Found live, the hard way (alfred msg 7402's real container run):
    # `systemctl set-property ... MemoryHigh=max` fails outright ("Failed
    # to parse MemoryHigh=max: Invalid argument") -- do_calm's own DIRECT
    # cgroupfs writes use "max" (the kernel file's own vocabulary), but
    # systemd's own set-property/unit-property syntax for "unbounded" is
    # "infinity". Two different roads, two different words for the same
    # concept -- pins the reset value AND that _autocalm_stance_squeeze's
    # own release path pulls from the same constant rather than a second,
    # separately hardcoded literal that could drift back to "max" again.
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    orig_set = ramsteind._systemctl_set_property
    orig_show = ramsteind._systemctl_show_property
    ramsteind._save_stance_touched([
        {"unit": "cap.scope", "prop": "MemoryHigh", "road": "root", "uid": None},
    ])
    seen_values = []
    ramsteind._systemctl_set_property = lambda unit, prop, val, uid=None: (
        seen_values.append(val) or (True, None))
    ramsteind._systemctl_show_property = lambda unit, prop, uid=None: "infinity"
    try:
        ramsteind.do_stance_rollback()
    finally:
        ramsteind._systemctl_set_property = orig_set
        ramsteind._systemctl_show_property = orig_show
    if seen_values != ["infinity"]:
        fails.append(f"MemoryHigh reset must use systemd's 'infinity', not"
                     f" cgroupfs's 'max': {seen_values}")


# --- apply_stance: load failure never applies partial state ----------------

def test_apply_stance_load_failure_rolls_back(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    _stance_file(tmp, "{ broken")
    orig_rollback = ramsteind.do_stance_rollback
    called = []
    ramsteind.do_stance_rollback = lambda: called.append(1) or {"ok": True, "results": []}
    try:
        state = {}
        result = ramsteind.apply_stance(dict(ramsteind.DEFAULTS), state)
    finally:
        ramsteind.do_stance_rollback = orig_rollback
    if result["ok"] or not called:
        fails.append("a stance file that fails to load must roll back and report, not apply")
    if state.get("error") is None:
        fails.append("apply_stance must record the load error in stance_state")


def test_apply_stance_zero_rules_is_noop(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "missing.json")
    state = {}
    result = ramsteind.apply_stance(dict(ramsteind.DEFAULTS), state)
    if not result["ok"] or result.get("rules", 0) != 0:
        fails.append(f"a missing/zero-rule stance should be a clean no-op: {result}")


# --- do_stance_plan: read-only preview --------------------------------------

def test_stance_plan_never_writes(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    _stance_file(tmp, {"rules": [
        {"match": {"comm": "postgres"}, "tier": "protect"},
        {"match": {"comm": "chrome"}, "tier": "expendable"},
        {"match": {"comm": "sandbox"}, "tier": "cap", "memory_high_pct_of_total": 25},
    ]})
    orig_leaves = ramsteind.classify_leaves
    orig_set = ramsteind._systemctl_set_property
    orig_memtotal = ramsteind._mem_total_bytes
    calls = []
    ramsteind._systemctl_set_property = lambda *a, **k: (calls.append((a, k)) or (True, None))
    ramsteind._mem_total_bytes = lambda: 10 * 1024**3
    ramsteind.classify_leaves = lambda rules: [
        {"path": "/pg", "unit": "pg", "tier": "protect", "rule_index": 0,
         "road": "root", "uid": None, "usage": 1 * 1024**3, "resident": 1 * 1024**3,
         "procs": [_proc(1, "postgres")]},
        {"path": "/chrome", "unit": "chrome", "tier": "expendable", "rule_index": 1,
         "road": "bridge", "uid": 1000, "usage": 2 * 1024**3, "resident": 2 * 1024**3,
         "procs": [_proc(2, "chrome")]},
        {"path": "/sandbox", "unit": "sandbox", "tier": "cap", "rule_index": 2,
         "road": "root", "uid": None, "usage": 500 * 1024**2, "resident": 500 * 1024**2,
         "procs": [_proc(3, "sandbox")]},
    ]
    try:
        doc = ramsteind.do_stance_plan(dict(ramsteind.DEFAULTS))
    finally:
        ramsteind.classify_leaves = orig_leaves
        ramsteind._systemctl_set_property = orig_set
        ramsteind._mem_total_bytes = orig_memtotal
    if calls:
        fails.append(f"stance plan must NEVER call _systemctl_set_property, but it did: {calls}")
    touched_after = ramsteind._load_stance_touched()
    if touched_after:
        fails.append(f"stance plan must never touch the persisted touched-cgroups list: {touched_after}")
    if "protect" not in doc or doc["protect"]["written"][0]["memory_low"] != 1 * 1024**3:
        fails.append(f"plan should compute the same protect numbers apply would: {doc.get('protect')}")
    if not doc.get("cap") or doc["cap"][0]["memory_high"] != int(10 * 1024**3 * 0.25):
        fails.append(f"plan should compute the same cap numbers apply would: {doc.get('cap')}")
    if not doc.get("expendable") or "no standing write" not in doc["expendable"][0]["note"]:
        fails.append(f"plan should explain expendable never gets a standing write: {doc.get('expendable')}")


def test_stance_plan_file_override(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    # the configured stance path stays empty -- plan must read the
    # EXPLICIT --file instead, e.g. previewing stance.example.json
    # before it's ever renamed into place.
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "configured-empty.json")
    draft = os.path.join(tmp, "draft.json")
    with open(draft, "w") as f:
        json.dump({"rules": [{"match": {"comm": "x"}, "tier": "protect"}]}, f)
    orig_leaves = ramsteind.classify_leaves
    ramsteind.classify_leaves = lambda rules: []
    try:
        doc = ramsteind.do_stance_plan(dict(ramsteind.DEFAULTS), file_path=draft)
    finally:
        ramsteind.classify_leaves = orig_leaves
    if doc.get("rules") != 1:
        fails.append(f"plan should read the --file override, not the configured (empty) path: {doc}")

    missing = os.path.join(tmp, "does-not-exist.json")
    doc2 = ramsteind.do_stance_plan(dict(ramsteind.DEFAULTS), file_path=missing)
    if not doc2.get("error"):
        fails.append("plan --file against a missing file must error, not silently read as zero rules")


# --- query_stance_status: the "error" key must never be present-but-None --

def test_stance_status_omits_error_key_when_clean(fails):
    # Found live, the hard way (alfred msg 7402's real container run):
    # request_or_die (ramstein CLI) treats KEY PRESENCE as failure
    # ("error" in doc), not truthiness. An earlier version of this
    # dispatch always returned {"error": None, ...} on a clean read,
    # which made `ramstein stance status` refuse unconditionally, on
    # every machine, forever -- no mocked unit test ever exercised the
    # actual socket response shape, only the daemon-side functions
    # directly.
    doc = ramsteind.query_stance_status(dict(ramsteind.DEFAULTS), {"error": None, "last_apply": None})
    if "error" in doc:
        fails.append(f"a clean status must not carry an 'error' key at all"
                     f" (even None) -- request_or_die treats presence as"
                     f" failure: {doc}")

    doc2 = ramsteind.query_stance_status(dict(ramsteind.DEFAULTS), {"error": "bad json", "last_apply": None})
    if doc2.get("error") != "bad json":
        fails.append(f"a real error must still surface: {doc2}")


# --- query_ledger: grouping -------------------------------------------------

def test_ledger_groups_unclassified_by_exe(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "missing.json")
    orig_leaves = ramsteind.classify_leaves
    ramsteind.classify_leaves = lambda rules: [
        {"path": "/a", "unit": "a", "tier": "unclassified", "rule_index": None,
         "road": "root", "uid": None, "usage": 100,
         "procs": [_proc(1, "python3", rss=50)]},
        {"path": "/b", "unit": "b", "tier": "unclassified", "rule_index": None,
         "road": "root", "uid": None, "usage": 200,
         "procs": [_proc(2, "python3", rss=80)]},
    ]
    orig_exe = ramsteind._proc_exe
    ramsteind._proc_exe = lambda pid: "/usr/bin/claude"
    try:
        doc = ramsteind.query_ledger(dict(ramsteind.DEFAULTS))
    finally:
        ramsteind.classify_leaves = orig_leaves
        ramsteind._proc_exe = orig_exe
    if len(doc["rows"]) != 1:
        fails.append(f"two unclassified leaves sharing an exe should collapse to ONE"
                     f" ledger row, got {len(doc['rows'])}")
    elif doc["rows"][0]["count"] != 2 or doc["rows"][0]["charged_bytes"] != 300:
        fails.append(f"grouped row totals wrong: {doc['rows'][0]}")


def test_ledger_labels_by_dominant_exe_with_others(fails):
    # alfred msg 7409/7427 item 3, live: "the fleet's terminal (29x
    # versions/2.1.261 + 14x rotten-apple-mcp + bash) is labelled
    # 'versions/2.1.260' count 1" -- a scope was labelled by whichever
    # pid happened to be listed first, not by what actually dominates
    # it. One scope here: 3 claude-version processes (dominant by
    # resident) + 1 unrelated cat -- must label by claude (normalized,
    # dropping the version segment), count the 3 claude processes, and
    # fold the cat into "others", never give the cat its own row or its
    # own label.
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "missing.json")
    orig_leaves = ramsteind.classify_leaves
    ramsteind.classify_leaves = lambda rules: [
        {"path": "/terminal", "unit": "terminal", "tier": "unclassified",
         "rule_index": None, "road": "root", "uid": None, "usage": 1000,
         # cat listed FIRST on purpose -- matches the real reported bug
         # (msg 7409) exactly: the scope's mislabeling came from picking
         # whichever pid /proc happened to enumerate first, not the
         # biggest. A test that put the dominant proc first wouldn't
         # actually distinguish "dominant by resident" from "first in
         # the list" -- confirmed by a negative control that left this
         # bug unfixed and still passed until the ordering was flipped.
         "procs": [_proc(4, "cat", rss=10), _proc(1, "claude", rss=300),
                  _proc(2, "claude", rss=300), _proc(3, "claude", rss=300)]},
    ]
    orig_exe = ramsteind._proc_exe
    ramsteind._proc_exe = lambda pid: (
        None if pid == 4 else "/home/x/.local/share/claude/versions/2.1.260")
    try:
        doc = ramsteind.query_ledger(dict(ramsteind.DEFAULTS))
    finally:
        ramsteind.classify_leaves = orig_leaves
        ramsteind._proc_exe = orig_exe
    if len(doc["rows"]) != 1:
        fails.append(f"one scope should still be one row, got {len(doc['rows'])}: {doc['rows']}")
    else:
        r = doc["rows"][0]
        if r["label"] != "/home/x/.local/share/claude/versions/*":
            fails.append(f"label should be the dominant exe, version-normalized: {r['label']}")
        if r["count"] != 3:
            fails.append(f"count should be the 3 dominant-exe processes, not all 4: {r}")
        if r.get("others") != 1:
            fails.append(f"the lone cat should fold into 'others', not vanish or get its own row: {r}")


def test_normalize_exe_label_strips_version_segment(fails):
    if ramsteind._normalize_exe_label("/home/x/.local/share/claude/versions/2.1.260") \
            != "/home/x/.local/share/claude/versions/*":
        fails.append("a trailing bare-version path segment should normalize to '*'")
    if ramsteind._normalize_exe_label("/opt/google/chrome/chrome") \
            != "/opt/google/chrome/chrome":
        fails.append("a normal exe path (no version-shaped final segment) must pass through unchanged")


def test_ledger_kernel_row_names_top_slab_holder(fails):
    # alfred's field finding (msg 7372): kernel slab (dentries/inodes) can
    # concentrate heavily in ONE scope and memory.current already counts
    # it toward that scope's charged total, but nothing NAMES it -- the
    # ledger's synthetic 'kernel' row is that name.
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "missing.json")
    orig_leaves = ramsteind.classify_leaves
    orig_slab = ramsteind._leaf_slab_bytes
    ramsteind.classify_leaves = lambda rules: [
        {"path": "/big-slab", "unit": "terminal", "tier": "unclassified",
         "rule_index": None, "road": "root", "uid": None, "usage": 1000,
         "procs": [_proc(1, "bash", rss=100)]},
        {"path": "/small-slab", "unit": "other", "tier": "unclassified",
         "rule_index": None, "road": "root", "uid": None, "usage": 2000,
         "procs": [_proc(2, "python3", rss=200)]},
    ]
    ramsteind._leaf_slab_bytes = lambda path: {
        "/big-slab": 4_200_000_000, "/small-slab": 100_000_000}.get(path, 0)
    try:
        doc = ramsteind.query_ledger(dict(ramsteind.DEFAULTS))
    finally:
        ramsteind.classify_leaves = orig_leaves
        ramsteind._leaf_slab_bytes = orig_slab
    kernel_rows = [r for r in doc["rows"] if r["tier"] == "kernel"]
    if len(kernel_rows) != 1:
        fails.append(f"expected exactly one synthetic kernel row, got {len(kernel_rows)}")
    elif kernel_rows[0]["charged_bytes"] != 4_200_000_000:
        fails.append(f"kernel row should name the BIGGEST slab holder"
                     f" (4.2G), not sum them: {kernel_rows[0]}")
    elif "bash" not in kernel_rows[0]["label"]:
        fails.append(f"kernel row should name which scope it came from: {kernel_rows[0]}")


def test_ledger_no_kernel_row_when_no_slab(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    os.environ["RAMSTEIN_STANCE_PATH"] = os.path.join(tmp, "missing.json")
    orig_leaves = ramsteind.classify_leaves
    orig_slab = ramsteind._leaf_slab_bytes
    ramsteind.classify_leaves = lambda rules: [
        {"path": "/x", "unit": "x", "tier": "unclassified", "rule_index": None,
         "road": "root", "uid": None, "usage": 100, "procs": [_proc(1, "x")]},
    ]
    ramsteind._leaf_slab_bytes = lambda path: 0
    try:
        doc = ramsteind.query_ledger(dict(ramsteind.DEFAULTS))
    finally:
        ramsteind.classify_leaves = orig_leaves
        ramsteind._leaf_slab_bytes = orig_slab
    if any(r["tier"] == "kernel" for r in doc["rows"]):
        fails.append("no leaf reported any slab -- there should be no kernel row at all")


def main():
    fails = []
    test_load_stance_missing_and_malformed(fails)
    test_match_leaf_unit_comm_exe_glob(fails)
    test_v1_parent_scope_bug_does_not_match_a_leaf(fails)
    test_unit_glob_wins_regardless_of_rule_order(fails)
    test_exe_match_uses_dominant_proc_only(fails)
    test_claude_in_chrome_shared_scope_resolves_expendable(fails)
    test_leaf_road_delegation_boundary(fails)
    test_ancestor_chain_for_protect(fails)
    test_protect_floor_ceiling(fails)
    test_rollback_resets_and_tracks_failures(fails)
    test_rollback_refuses_loudly_when_touched_list_unreadable(fails)
    test_rollback_memory_high_uses_systemd_infinity_not_cgroupfs_max(fails)
    test_apply_stance_load_failure_rolls_back(fails)
    test_apply_stance_zero_rules_is_noop(fails)
    test_stance_plan_never_writes(fails)
    test_stance_plan_file_override(fails)
    test_stance_status_omits_error_key_when_clean(fails)
    test_ledger_groups_unclassified_by_exe(fails)
    test_ledger_labels_by_dominant_exe_with_others(fails)
    test_normalize_exe_label_strips_version_segment(fails)
    test_ledger_kernel_row_names_top_slab_holder(fails)
    test_ledger_no_kernel_row_when_no_slab(fails)

    if fails:
        print("STANCE TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("stance ok: load/validate, leaf-only classification (unit_glob/"
          "comm/exe_glob, first-match-wins, unclassified fallback), the "
          "delegation-boundary road split, the full protect ancestor "
          "chain, the floor/ceiling pin, rollback bookkeeping, load-"
          "failure rollback, ledger grouping (including unclassified "
          "by exe), and the kernel-slab synthetic row (biggest holder "
          "only, absent when nothing reports slab) all hold")


if __name__ == "__main__":
    main()
