#!/usr/bin/env python3
"""
Design pass on proc_min_bytes (thread 3dd73060, alfred's dispatch DM 4549):
the floor's purpose is daemon cost (bound the per-sample /proc walk + sqlite
insert count), never a relevance judgment -- but a process under it used to
be dropped with zero trace, which is practice 2c45d78e's exact trap: the
surface asserted "nothing here" when the honest claim was "nothing above
16 MiB". Measured live on the operator's own machine: 272 of 278 processes
fell under the default floor in one ordinary sample -- not an edge case.

Fixed by disclosure, not a different number (alfred's own instruction:
"I'd rather have a written finding than a changed constant"). This file
tests three things:

1. Sampler._sample() computes and persists below_floor correctly against a
   synthetic process list (monkeypatched _read_procs, deterministic sizes
   straddling the floor -- not real /proc, which can't be controlled).
2. samples.below_floor survives on a database created BEFORE this column
   existed -- _ensure_below_floor_column migrates it via ALTER TABLE
   rather than assuming every install starts fresh.
3. query_top/query_swap/query_blame surface it in their own response
   shape, matching what the CLI now prints a caveat line from.

Run as: python3 tests/test_proc_min_bytes.py
"""
import atexit
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-proc-min-bytes-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)


def _fake_proc(pid, comm, rss, swap=0, state="S"):
    return {"pid": pid, "comm": comm, "rss": rss, "swap": swap,
            "state": state, "ppid": 1, "starttime": 100 + pid}


def _default_cfg():
    return dict(ramsteind.DEFAULTS)


def test_below_floor_computed_and_persisted(tmp):
    """Six processes, three clear the default 16 MiB floor (rss+swap),
    three don't -- below_floor must read exactly 3, and the three that
    cleared it must be the only rows written."""
    fails = []
    old_state = os.environ.get("RAMSTEIN_STATE_DIR")
    os.environ["RAMSTEIN_STATE_DIR"] = tmp
    ramsteind.STATE_DIR = tmp
    ramsteind.DB_PATH = os.path.join(tmp, "index.db")
    floor = ramsteind.DEFAULTS["proc_min_bytes"]
    procs = [
        _fake_proc(101, "big-one", rss=floor),
        _fake_proc(102, "big-two", rss=floor // 2, swap=floor // 2),
        _fake_proc(103, "big-three", rss=floor + 1024),
        _fake_proc(201, "tiny-one", rss=1024),
        _fake_proc(202, "tiny-two", rss=floor - 1),
        _fake_proc(203, "tiny-three", rss=0, swap=0),
    ]
    orig_read_procs = ramsteind._read_procs
    ramsteind._read_procs = lambda: iter(procs)
    try:
        cfg = _default_cfg()
        cfg["sample_every"] = 1
        sampler = ramsteind.Sampler(cfg)
        sampler._sample(1000.0)
        con = ramsteind._connect_index()
        try:
            row = con.execute(
                "SELECT below_floor FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row is None:
                fails.append("no sample row written")
            elif row[0] != 3:
                fails.append(f"expected below_floor=3, got {row[0]!r}")
            kept = {r[0] for r in con.execute(
                "SELECT pid FROM proc_stats")}
            if kept != {101, 102, 103}:
                fails.append(f"expected pids {{101,102,103}} indexed,"
                              f" got {kept!r}")
        finally:
            con.close()
    finally:
        ramsteind._read_procs = orig_read_procs
        if old_state is None:
            os.environ.pop("RAMSTEIN_STATE_DIR", None)
        else:
            os.environ["RAMSTEIN_STATE_DIR"] = old_state
    return fails


def test_migration_adds_column_to_a_preexisting_database(tmp):
    """An already-installed database predates this column -- CREATE TABLE
    IF NOT EXISTS never touches an existing table's shape, so a real
    upgrade needs _ensure_below_floor_column's ALTER TABLE, not just the
    schema string. Build a DB with the OLD (pre-pass) samples shape by
    hand, then confirm Sampler.__init__ migrates it without losing the
    row already there."""
    fails = []
    db_path = os.path.join(tmp, "migration.db")
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE samples(
                id INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                promoted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE proc_stats(
                sample_id INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                starttime INTEGER NOT NULL,
                comm TEXT NOT NULL,
                rss INTEGER NOT NULL,
                swap INTEGER NOT NULL,
                state TEXT NOT NULL,
                ppid INTEGER NOT NULL,
                PRIMARY KEY(sample_id, pid, starttime)) WITHOUT ROWID;
        """)
        con.execute("INSERT INTO samples(ts, promoted) VALUES(?,?)",
                    (500.0, 0))
        con.commit()
    finally:
        con.close()

    old_state = os.environ.get("RAMSTEIN_STATE_DIR")
    os.environ["RAMSTEIN_STATE_DIR"] = tmp
    ramsteind.STATE_DIR = tmp
    ramsteind.DB_PATH = db_path
    try:
        ramsteind.Sampler(_default_cfg())  # __init__ runs the migration
        con = ramsteind._connect_index()
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
            if "below_floor" not in cols:
                fails.append("migration did not add below_floor")
                return fails
            row = con.execute(
                "SELECT ts, below_floor FROM samples").fetchone()
            if row is None:
                fails.append("pre-existing sample row was lost")
            elif row != (500.0, 0):
                fails.append(f"pre-existing row corrupted: {row!r}"
                              f" (expected (500.0, 0) -- DEFAULT 0"
                              f" backfilled)")
        finally:
            con.close()
        # idempotent: running the migration again against an already-
        # migrated database must not raise (duplicate column error)
        con2 = ramsteind._connect_index()
        try:
            ramsteind._ensure_below_floor_column(con2)
        finally:
            con2.close()
    finally:
        if old_state is None:
            os.environ.pop("RAMSTEIN_STATE_DIR", None)
        else:
            os.environ["RAMSTEIN_STATE_DIR"] = old_state
    return fails


def test_queries_surface_below_floor(tmp):
    """query_top/query_swap/query_blame all thread below_floor through
    to their own response shape -- this is what the CLI's caveat line
    reads from, so a regression here is silent at the daemon layer and
    loud (a missing warning) at the CLI layer."""
    fails = []
    old_state = os.environ.get("RAMSTEIN_STATE_DIR")
    os.environ["RAMSTEIN_STATE_DIR"] = tmp
    ramsteind.STATE_DIR = tmp
    ramsteind.DB_PATH = os.path.join(tmp, "index.db")
    floor = ramsteind.DEFAULTS["proc_min_bytes"]
    base_procs = [_fake_proc(101, "steady", rss=floor),
                  _fake_proc(201, "tiny", rss=100)]
    head_procs = [_fake_proc(101, "steady", rss=floor),
                  _fake_proc(202, "tiny2", rss=100),
                  _fake_proc(203, "tiny3", rss=100)]
    orig_read_procs = ramsteind._read_procs
    try:
        cfg = _default_cfg()
        cfg["sample_every"] = 1
        sampler = ramsteind.Sampler(cfg)
        ramsteind._read_procs = lambda: iter(base_procs)
        sampler._sample(1000.0)
        ramsteind._read_procs = lambda: iter(head_procs)
        sampler._sample(1010.0)

        top = ramsteind.query_top(by_swap=False, limit=20)
        if top.get("below_floor") != 2:
            fails.append(f"query_top: expected below_floor=2 (head sample),"
                          f" got {top.get('below_floor')!r}")

        swap = ramsteind.query_swap(limit=20)
        if swap.get("below_floor") != 2:
            fails.append(f"query_swap: expected below_floor=2,"
                          f" got {swap.get('below_floor')!r}")

        blame = ramsteind.query_blame(since=1005.0, limit=20)
        if blame.get("base_below_floor") != 1:
            fails.append(f"query_blame: expected base_below_floor=1,"
                          f" got {blame.get('base_below_floor')!r}")
        if blame.get("head_below_floor") != 2:
            fails.append(f"query_blame: expected head_below_floor=2,"
                          f" got {blame.get('head_below_floor')!r}")
    finally:
        ramsteind._read_procs = orig_read_procs
        if old_state is None:
            os.environ.pop("RAMSTEIN_STATE_DIR", None)
        else:
            os.environ["RAMSTEIN_STATE_DIR"] = old_state
    return fails


def main():
    all_fails = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, fn in [
            ("below_floor computed and persisted", test_below_floor_computed_and_persisted),
            ("migration adds the column to a pre-existing database",
             test_migration_adds_column_to_a_preexisting_database),
            ("queries surface below_floor", test_queries_surface_below_floor),
        ]:
            d = os.path.join(tmp, name.replace(" ", "_"))
            os.makedirs(d, exist_ok=True)
            fails = fn(d)
            if fails:
                all_fails.append((name, fails))
            else:
                print(f"proc_min_bytes: {name} ok")

    if all_fails:
        print("PROC_MIN_BYTES TEST FAILED:")
        for name, fails in all_fails:
            for f in fails:
                print(f"  - [{name}] {f}")
        sys.exit(1)
    print("proc_min_bytes ok: below_floor computed correctly, an"
          " already-installed database migrates cleanly, and"
          " top/swap/blame all surface it")


if __name__ == "__main__":
    main()
