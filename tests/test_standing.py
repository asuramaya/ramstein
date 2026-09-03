#!/usr/bin/env python3
"""
Tests V3's `standing` report -- "stock, not flow" (alfred's ruling, msg
6644, following the operator's "keep digging" past the notification gap):
ramstein measures rates (burn, PSI, ETA, swap velocity), and a rate-
watcher is structurally blind to a pile that has stopped growing.

Three independently-gated tiers:

1. query_standing_stock: blame's exact inverse over the SAME sampled
   index -- a process counts only if it's present in BOTH the base and
   head samples (a genuinely new process can't have been flat for a
   day), clears standing_min_bytes, and hasn't moved more than
   standing_flat_pct since a sample at least standing_window_hours ago.
   Built against a SYNTHETIC sqlite fixture (deterministic sizes/deltas
   straddling every threshold), not real /proc, which can't be
   controlled -- same discipline as test_proc_min_bytes.py.

2. _pathless_shared_vma / query_standing_anon: the anonymous/memfd
   attribution byebyte can never duplicate. Unit-tested against the
   EXACT real VMA path shapes captured live on the operator's own
   machine (2026-09-03): chrome's /dev/shm/.com.google.Chrome.*
   (deleted) segments are tmpfs-backed and must be excluded; pulseaudio/
   wayland's /memfd:pulseaudio (deleted) segments have no backing mount
   and must be included.

3. the swap watermark: persists to disk, peak-only-increases, and a
   fresh install bootstraps rather than crashing.

Run as: python3 tests/test_standing.py
"""
import atexit
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-standing-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)


# --- tier 1: stock -----------------------------------------------------------

def _build_index(tmp, now):
    """base sample at now-25h (older than the 24h default window), head
    sample at now. Six proc_stats rows exercise every boundary:
      steady-big    : 1000M -> 1010M  (1% delta, clears 500M floor)  -> STOCK
      grew-big      : 1000M -> 1300M  (30% delta)                    -> not flat
      steady-small  : 100M  -> 101M   (flat, but under the 500M floor)-> too small
      new-big       : absent -> 800M  (present only in head)         -> "new", not stock
      gone-big      : 900M  -> absent (present only in base)         -> irrelevant to stock
      steady-huge   : 2000M -> 2000M  (0% delta)                     -> STOCK
    """
    ramsteind.STATE_DIR = tmp
    ramsteind.DB_PATH = os.path.join(tmp, "index.db")
    con = sqlite3.connect(ramsteind.DB_PATH)
    con.executescript(ramsteind._SCHEMA)
    base_ts = now - 25 * 3600
    con.execute("INSERT INTO samples(id, ts, promoted, below_floor)"
                " VALUES (1, ?, 1, 0)", (base_ts,))
    con.execute("INSERT INTO samples(id, ts, promoted, below_floor)"
                " VALUES (2, ?, 0, 0)", (now,))
    MB = 1024 * 1024
    base_rows = [
        (1, 5001, 1, "steady-big", 1000 * MB, 0, "S", 1),
        (1, 5002, 1, "grew-big", 1000 * MB, 0, "S", 1),
        (1, 5003, 1, "steady-small", 100 * MB, 0, "S", 1),
        (1, 5005, 1, "gone-big", 900 * MB, 0, "S", 1),
        (1, 5006, 1, "steady-huge", 2000 * MB, 0, "S", 1),
    ]
    head_rows = [
        (2, 5001, 1, "steady-big", 1010 * MB, 0, "S", 1),
        (2, 5002, 1, "grew-big", 1300 * MB, 0, "S", 1),
        (2, 5003, 1, "steady-small", 101 * MB, 0, "S", 1),
        (2, 5004, 1, "new-big", 800 * MB, 0, "S", 1),
        (2, 5006, 1, "steady-huge", 2000 * MB, 0, "S", 1),
    ]
    con.executemany(
        "INSERT INTO proc_stats(sample_id, pid, starttime, comm, rss, swap,"
        " state, ppid) VALUES (?,?,?,?,?,?,?,?)", base_rows + head_rows)
    con.commit()
    con.close()


def test_stock_boundaries(fails):
    import time
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    now = time.time()
    _build_index(tmp, now)
    cfg = dict(ramsteind.DEFAULTS)
    doc = ramsteind.query_standing_stock(cfg, limit=20)
    if "error" in doc:
        fails.append(f"stock query errored unexpectedly: {doc}")
        return
    names = {r["comm"] for r in doc["rows"]}
    want = {"steady-big", "steady-huge"}
    if names != want:
        fails.append(f"stock rows = {names}, want exactly {want}"
                      f" (grew-big must fail flatness, steady-small must fail"
                      f" the size floor, new-big/gone-big must be excluded"
                      f" as not present in both samples): {doc['rows']}")


def test_stock_not_enough_history(fails):
    import time
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    ramsteind.STATE_DIR = tmp
    ramsteind.DB_PATH = os.path.join(tmp, "index.db")
    con = sqlite3.connect(ramsteind.DB_PATH)
    con.executescript(ramsteind._SCHEMA)
    now = time.time()
    # only ONE sample, five minutes old -- nowhere near the 24h window.
    con.execute("INSERT INTO samples(id, ts, promoted, below_floor)"
                " VALUES (1, ?, 0, 0)", (now - 300,))
    con.commit()
    con.close()
    doc = ramsteind.query_standing_stock(dict(ramsteind.DEFAULTS), limit=20)
    if "error" not in doc:
        fails.append(f"expected an honest 'not enough history' error with"
                      f" only one recent sample, got: {doc}")


# --- tier 2: anonymous-memory attribution ------------------------------------

# Real VMA path shapes captured live (2026-09-03) -- not invented.
_TMPFS_MOUNTS = ["/run", "/dev/shm", "/tmp", "/run/user/1000"]


def test_pathless_shared_vma_classification(fails):
    cases = [
        ("", True, "bare anonymous mmap (no path at all)"),
        ("/memfd:pulseaudio (deleted)", True, "memfd, no backing mount -- the exact unattributed remainder"),
        ("/memfd:wayland-cursor (deleted)", True, "memfd, no backing mount"),
        ("/SYSV00000000", True, "SysV shm, no backing mount"),
        ("/dev/shm/.com.google.Chrome.gi3fd8 (deleted)", False, "tmpfs-backed (even though unlinked) -- already in _tmpfs_reachable_bytes"),
        ("/dev/shm/some-live-file", False, "tmpfs-backed, live"),
        ("/tmp/pt-2654420/somefile", False, "tmpfs-backed under /tmp"),
        ("/usr/lib/x86_64-linux-gnu/libc.so.6", False, "an ordinary shared library -- not memory ramstein should attribute here"),
    ]
    for path, want, why in cases:
        got = ramsteind._pathless_shared_vma(path, _TMPFS_MOUNTS)
        if got != want:
            fails.append(f"_pathless_shared_vma({path!r}) = {got}, want {want}"
                          f" ({why})")


def test_anon_shared_bytes_real_process(fails):
    """Exercise the real per-VMA smaps parser against THIS process (self)
    rather than a fixture -- /proc/self/smaps is real, live, and always
    available, unlike another process's, which needs same-uid or root."""
    result = ramsteind._anon_shared_bytes(os.getpid(), _TMPFS_MOUNTS)
    if result is None:
        fails.append("_anon_shared_bytes(self) returned None against a"
                      " definitely-readable /proc/self/smaps")
    elif not isinstance(result, int) or result < 0:
        fails.append(f"_anon_shared_bytes(self) returned a non-sensible"
                      f" value: {result!r}")


def test_anon_shared_bytes_missing_pid(fails):
    """A pid that doesn't exist -- None, never a fabricated zero (a
    process that exited mid-scan must read as 'skip this one', not as
    'this process holds 0 bytes of anonymous memory')."""
    result = ramsteind._anon_shared_bytes(999999999, _TMPFS_MOUNTS)
    if result is not None:
        fails.append(f"expected None for a nonexistent pid, got {result!r}")


# --- tier 3: swap watermark ---------------------------------------------------

def test_swap_watermark_bootstraps_and_persists(fails):
    import time
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    ramsteind.STATE_DIR = tmp
    ramsteind.SWAP_WATERMARK_PATH = os.path.join(tmp, "swap_watermark.json")
    now = time.time()

    # first call ever: bootstraps to the CURRENT usage, doesn't crash on a
    # missing file.
    doc = ramsteind.update_swap_watermark(now, swap_total=38 * 1024**3,
                                          swap_free=38 * 1024**3)
    if doc["peak_used_bytes"] != 0:
        fails.append(f"bootstrap with 0 used should record peak 0, got {doc}")
    if not os.path.exists(ramsteind.SWAP_WATERMARK_PATH):
        fails.append("watermark wasn't persisted to disk on first call")

    # usage rises -- peak must track it.
    doc = ramsteind.update_swap_watermark(
        now + 60, swap_total=38 * 1024**3, swap_free=(38 - 2) * 1024**3)
    if doc["peak_used_bytes"] != 2 * 1024**3:
        fails.append(f"peak didn't rise to the new high: {doc}")

    # usage falls back to 0 -- peak must NOT fall; a watermark that drops
    # back down would misreport a real historical peak as "never used".
    doc = ramsteind.update_swap_watermark(
        now + 120, swap_total=38 * 1024**3, swap_free=38 * 1024**3)
    if doc["peak_used_bytes"] != 2 * 1024**3:
        fails.append(f"peak fell when usage dropped -- watermarks must only"
                      f" ever rise: {doc}")

    # a FRESH process (simulating a daemon restart) reads the persisted
    # peak back rather than re-bootstrapping to the current (now-lower)
    # usage.
    reread = ramsteind._load_swap_watermark()
    if reread["peak_used_bytes"] != 2 * 1024**3:
        fails.append(f"restart didn't read the persisted peak back: {reread}")


# --- combiner -----------------------------------------------------------------

def test_query_standing_combines_all_three(fails):
    """query_standing must never let one tier's failure block the
    others -- a fresh install with no 24h history yet should still
    answer the anon and swap tiers."""
    tmp = tempfile.mkdtemp(dir=_STATE_FIXTURE)
    ramsteind.STATE_DIR = tmp
    ramsteind.DB_PATH = os.path.join(tmp, "index.db")
    ramsteind.SWAP_WATERMARK_PATH = os.path.join(tmp, "swap_watermark.json")
    con = sqlite3.connect(ramsteind.DB_PATH)
    con.executescript(ramsteind._SCHEMA)
    con.commit()
    con.close()
    doc = ramsteind.query_standing(dict(ramsteind.DEFAULTS))
    for key in ("stock", "anon", "swap_watermark", "tmpfs_reachable_bytes"):
        if key not in doc:
            fails.append(f"query_standing's response missing {key!r}: {doc}")
    if "error" not in doc.get("stock", {}):
        fails.append("expected stock to honestly report no history yet on"
                      " a brand-new index, got real rows instead")
    if "rows" not in doc.get("anon", {}):
        fails.append(f"anon tier should still answer despite stock having"
                      f" no history: {doc.get('anon')}")


def main():
    fails = []
    test_stock_boundaries(fails)
    test_stock_not_enough_history(fails)
    test_pathless_shared_vma_classification(fails)
    test_anon_shared_bytes_real_process(fails)
    test_anon_shared_bytes_missing_pid(fails)
    test_swap_watermark_bootstraps_and_persists(fails)
    test_query_standing_combines_all_three(fails)

    if fails:
        print("STANDING TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("standing ok: stock finds exactly the flat-and-large processes"
          " (excluding too-small, too-volatile, new, and gone), honestly"
          " refuses with too little history, the anon-memory VMA"
          " classifier matches real live path shapes, the smaps walker"
          " handles a real process and a missing one, the swap watermark"
          " bootstraps/persists/only-ever-rises, and the combiner never"
          " lets one tier's gap block the others")


if __name__ == "__main__":
    main()
