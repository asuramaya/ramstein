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
  - _apply_protect_tier: floor sized to the sum of protect-tier usage,
    capped at stance_protect_floor_ceiling_pct of MemTotal, and the
    ledger-visible "pinned" flag when a leak would otherwise ratchet it
    past that ceiling.
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
        # under ceiling: floor tracks real usage.
        leaves = [{"path": "/a", "unit": "a", "usage": 1 * 1024**3, "road": "root", "uid": None},
                  {"path": "/b", "unit": "b", "usage": 2 * 1024**3, "road": "root", "uid": None}]
        touched = []
        result = ramsteind._apply_protect_tier(cfg, leaves, touched)
        if result["pinned_at_ceiling"]:
            fails.append("3G of 10G (30%, under the 50% default ceiling) should not be pinned")
        if result["floor_bytes"] != 3 * 1024**3:
            fails.append(f"floor should track real usage under the ceiling: {result['floor_bytes']}")

        # over ceiling (a "leak"): floor pins at 50% of MemTotal, not the
        # unbounded sum -- alfred msg 7125 note 1.
        calls.clear()
        leaves = [{"path": "/a", "unit": "a", "usage": 8 * 1024**3, "road": "root", "uid": None}]
        touched = []
        result = ramsteind._apply_protect_tier(cfg, leaves, touched)
        if not result["pinned_at_ceiling"]:
            fails.append("8G of 10G should exceed the 50% ceiling and pin")
        if result["floor_bytes"] != 5 * 1024**3:
            fails.append(f"pinned floor should be exactly 50% of MemTotal: {result['floor_bytes']}")
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


def main():
    fails = []
    test_load_stance_missing_and_malformed(fails)
    test_match_leaf_unit_comm_exe_glob(fails)
    test_v1_parent_scope_bug_does_not_match_a_leaf(fails)
    test_leaf_road_delegation_boundary(fails)
    test_ancestor_chain_for_protect(fails)
    test_protect_floor_ceiling(fails)
    test_rollback_resets_and_tracks_failures(fails)
    test_apply_stance_load_failure_rolls_back(fails)
    test_apply_stance_zero_rules_is_noop(fails)
    test_ledger_groups_unclassified_by_exe(fails)

    if fails:
        print("STANCE TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("stance ok: load/validate, leaf-only classification (unit_glob/"
          "comm/exe_glob, first-match-wins, unclassified fallback), the "
          "delegation-boundary road split, the full protect ancestor "
          "chain, the floor/ceiling pin, rollback bookkeeping, load-"
          "failure rollback, and ledger grouping (including unclassified "
          "by exe) all hold")


if __name__ == "__main__":
    main()
