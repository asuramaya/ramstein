#!/usr/bin/env python3
"""
Tests V3's incident snapshot -- "fire marshal, not firefighter" (alfred's
ruling, msg 7068/7077, 2026-09-05). standing/the swap watermark could only
ever SAY a peak happened; neither could answer "who was resident when it
did" after the fact -- the operator's own field case: a real, unexplained
20.2G/40G (50.5%) swap peak that ramstein had only ever reported as a bare
number in the watermark line.

Covers:
  - each of the three triggers (swap_pct, psi_full, swap_storm) fires on
    the RISING edge and does not re-fire while the condition holds --
    same hysteresis discipline as swap_storm's own, applied to the entry
    side rather than the exit side.
  - a trigger that clears and re-crosses fires again (a second, distinct
    incident, not swallowed by stale state).
  - multiple triggers on the same tick collapse into ONE record naming
    all of them, not one record each.
  - _append_incident's retention trim keeps only the most recent
    incident_max_records, oldest-first-dropped.
  - query_incidents reads most-recent-first, tolerates a missing file and
    a torn last line (a daemon killed mid-write), and never crashes.

Run as: python3 tests/test_incidents.py
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

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-incidents-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)


def _mem(swap_total=40 * 1024**3, swap_used_pct=0.0, avail=8 * 1024**3,
         psi_full=0.0):
    swap_free = swap_total - int(swap_total * swap_used_pct / 100.0)
    return {"available": avail, "swap_total": swap_total,
            "swap_free": swap_free, "psi": {"full_avg10": psi_full}}


def _reset(tmp):
    ramsteind.STATE_DIR = tmp
    ramsteind.INCIDENTS_PATH = os.path.join(tmp, "incidents.jsonl")
    # no real index in this tmp dir -- _top_residents_by_rss_swap will
    # hit _connect_index/_latest_sample against an empty/missing db;
    # patch it out so these tests exercise the TRIGGER logic, not the
    # sampler (that's test_standing.py's own job for the same primitive).
    ramsteind._top_residents_by_rss_swap = lambda limit: []


def test_swap_pct_edge_fires_once(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    cfg = dict(ramsteind.DEFAULTS)
    state = {}
    now = 1000.0

    # below threshold: no incident.
    ramsteind.check_incident(state, cfg, now, _mem(swap_used_pct=10.0), None)
    if ramsteind.query_incidents(20)["rows"]:
        fails.append("fired below the swap_pct threshold")

    # crosses: fires exactly once.
    ramsteind.check_incident(state, cfg, now + 10, _mem(swap_used_pct=60.0), None)
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 1:
        fails.append(f"expected exactly 1 incident on the crossing tick, got {len(rows)}")
    elif rows[0]["triggers"] != [{"kind": "swap_pct", "value": 60.0, "threshold": 50.0}]:
        fails.append(f"trigger record wrong shape: {rows[0]['triggers']}")

    # stays crossed: must NOT re-fire.
    ramsteind.check_incident(state, cfg, now + 20, _mem(swap_used_pct=61.0), None)
    ramsteind.check_incident(state, cfg, now + 30, _mem(swap_used_pct=65.0), None)
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 1:
        fails.append(f"re-fired while still crossed: {len(rows)} incidents")

    # clears, then re-crosses: a SECOND, distinct incident.
    ramsteind.check_incident(state, cfg, now + 40, _mem(swap_used_pct=20.0), None)
    ramsteind.check_incident(state, cfg, now + 50, _mem(swap_used_pct=55.0), None)
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 2:
        fails.append(f"re-crossing after clearing should be a new incident,"
                      f" got {len(rows)} total")


def test_psi_full_edge(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    cfg = dict(ramsteind.DEFAULTS)
    state = {}
    ramsteind.check_incident(state, cfg, 100.0, _mem(psi_full=1.0), None)
    ramsteind.check_incident(state, cfg, 110.0,
                             _mem(psi_full=cfg["hot_psi_full"] + 1.0), None)
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 1 or rows[0]["triggers"][0]["kind"] != "psi_full":
        fails.append(f"psi_full trigger didn't fire correctly: {rows}")
    # stays hot: no re-fire.
    ramsteind.check_incident(state, cfg, 120.0,
                             _mem(psi_full=cfg["hot_psi_full"] + 2.0), None)
    if len(ramsteind.query_incidents(20)["rows"]) != 1:
        fails.append("psi_full re-fired while still hot")


def test_swap_storm_edge(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    cfg = dict(ramsteind.DEFAULTS)
    state = {}
    storm_doc = {"kind": "swap_storm", "eta_oom_seconds": 300}
    ramsteind.check_incident(state, cfg, 100.0, _mem(), None)  # inactive
    ramsteind.check_incident(state, cfg, 110.0, _mem(), storm_doc)  # becomes active
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 1 or rows[0]["triggers"][0]["kind"] != "swap_storm":
        fails.append(f"swap_storm trigger didn't fire on activation: {rows}")
    # still active: no re-fire.
    ramsteind.check_incident(state, cfg, 120.0, _mem(), storm_doc)
    if len(ramsteind.query_incidents(20)["rows"]) != 1:
        fails.append("swap_storm re-fired while still active")


def test_multiple_triggers_one_record(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    cfg = dict(ramsteind.DEFAULTS)
    state = {}
    mem = _mem(swap_used_pct=70.0, psi_full=cfg["hot_psi_full"] + 5.0)
    ramsteind.check_incident(state, cfg, 100.0, mem, None)
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 1:
        fails.append(f"two simultaneous crossings should be ONE record,"
                      f" got {len(rows)}")
    elif {t["kind"] for t in rows[0]["triggers"]} != {"swap_pct", "psi_full"}:
        fails.append(f"combined record missing a trigger: {rows[0]['triggers']}")


def test_retention_trim(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    for i in range(10):
        ramsteind._append_incident({"ts": float(i), "triggers": [],
                                    "available": 0, "swap_total": 0,
                                    "swap_free": 0, "psi": {}, "residents": []},
                                   max_records=3)
    rows = ramsteind.query_incidents(20)["rows"]
    tss = sorted(r["ts"] for r in rows)
    if len(rows) != 3 or tss != [7.0, 8.0, 9.0]:
        fails.append(f"retention trim kept the wrong set: {tss} (want the"
                      f" 3 most recent: [7.0, 8.0, 9.0])")


def test_query_incidents_missing_and_torn(fails):
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    _reset(tmp)
    # missing file entirely
    if ramsteind.query_incidents(20)["rows"] != []:
        fails.append("missing incidents.jsonl should read as empty, not error")
    # a torn last line (daemon killed mid-write) must not crash the read,
    # and the good lines before it must still come back.
    with open(ramsteind.INCIDENTS_PATH, "w") as f:
        f.write(json.dumps({"ts": 1.0, "triggers": [], "available": 0,
                            "swap_total": 0, "swap_free": 0, "psi": {},
                            "residents": []}) + "\n")
        f.write('{"ts": 2.0, "triggers": [] "swap_tot')  # torn, no newline
    rows = ramsteind.query_incidents(20)["rows"]
    if len(rows) != 1 or rows[0]["ts"] != 1.0:
        fails.append(f"a torn last line should be skipped, not crash the read"
                      f" or drop the good line before it: {rows}")


def main():
    fails = []
    test_swap_pct_edge_fires_once(fails)
    test_psi_full_edge(fails)
    test_swap_storm_edge(fails)
    test_multiple_triggers_one_record(fails)
    test_retention_trim(fails)
    test_query_incidents_missing_and_torn(fails)

    if fails:
        print("INCIDENTS TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("incidents ok: swap_pct/psi_full/swap_storm each fire once on the"
          " rising edge and re-fire only after clearing and re-crossing,"
          " simultaneous triggers collapse to one record, retention keeps"
          " only the most recent N, and reads tolerate a missing file and a"
          " torn last line")


if __name__ == "__main__":
    main()
