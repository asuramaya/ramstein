#!/usr/bin/env python3
"""
Tests RAMstein's layer-3 swap-size verb (decision f8e7cc5a): BUTTON
presets, BOUNDED-WAIT, PERSISTENT. A standalone backing file (STATE_DIR/
extra.img) + a standalone, statically-shipped systemd .swap unit --
`set` runs the real fallocate/mkswap/systemctl-enable sequence on a
background thread and reports `pending` immediately (FAMILY.md: "a verb
that may take minutes is not a control without a progress state"),
`status` is how the caller learns the real outcome.

The fake systemctl/mkswap/swapon trio models the mechanism, not a fixed
canned answer: fake systemctl's `enable --now` stats the REAL backing
file (truncated for real by the code under test, in a real tmpdir) and
writes a marker `swapon --show` then reads back -- so the size the test
sees really did flow through truncate -> mkswap -> enable -> swapon,
same shape as test_oomd_enroll.py's marker-file pattern.

Fulcrum negative control, twice: mkswap failing must surface as ok=False
(not silently continue to enable), and a systemctl enable that exits 0
without actually making swapon report the new size (the "world didn't
move" case ruling 41b72476 exists for) must also come back ok=False.

Run as: python3 tests/test_swap_size.py
"""
import atexit
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-swap-size-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

CFG = dict(ramsteind.DEFAULTS)
BACKING_PATH = ramsteind._swap_size_backing_file_path()


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def _fakebin(tmp, marker_path, mkswap_fail=False, enable_noop=False):
    """systemctl stop/disable clear the marker; enable --now stats the
    REAL backing file (truncated by the code under test) and writes a
    swapon(8)-shaped line into the marker -- UNLESS enable_noop, which
    exits 0 without writing it (systemctl "succeeding" while the world
    doesn't move, the exact case ruling 41b72476 exists to catch).
    mkswap_fail makes mkswap exit nonzero, so the sequence must stop
    there and never even reach the enable step."""
    d = tempfile.mkdtemp(dir=tmp)
    enable_body = "exit 0" if enable_noop else (
        f'size=$(stat -c%s "{BACKING_PATH}" 2>/dev/null || echo 0)\n'
        f'echo "{BACKING_PATH} file $size 0 -1" > "{marker_path}"\n'
        f'exit 0')
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
if [ "$1" = "daemon-reload" ]; then exit 0; fi
if [ "$1" = "stop" ]; then rm -f "{marker_path}"; exit 0; fi
if [ "$1" = "disable" ] && [ "$2" = "--now" ]; then rm -f "{marker_path}"; exit 0; fi
if [ "$1" = "enable" ] && [ "$2" = "--now" ]; then
{enable_body}
fi
exit 1
""")
    mkswap_body = 'echo "mkswap: simulated failure" >&2; exit 1' if mkswap_fail else "exit 0"
    _write_exec(os.path.join(d, "mkswap"), f"#!/usr/bin/env bash\n{mkswap_body}\n")
    _write_exec(os.path.join(d, "swapon"), f"""#!/usr/bin/env bash
[ -f "{marker_path}" ] && cat "{marker_path}"
exit 0
""")
    return d


def _wait_done(swap_size_state, timeout=10):
    start = time.time()
    while swap_size_state.get("in_progress") and time.time() - start < timeout:
        time.sleep(0.02)
    return swap_size_state.get("last_result")


def _fresh_state():
    return {"in_progress": False, "requested": None, "last_result": None}


def main():
    fails = []
    old_path = os.environ.get("PATH", "")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "swapon-marker")

            # --- range floor -------------------------------------------------
            state = _fresh_state()
            r = ramsteind.do_swap_size_set(CFG, 1024, state, dry_run=False)
            if "error" not in r or state["in_progress"]:
                fails.append(f"below-floor size not rejected: {r!r}")

            # --- disk headroom refusal (real statvfs on this real tmpdir,
            # no mock needed -- an absurd size can't possibly fit) ----------
            state = _fresh_state()
            r = ramsteind.do_swap_size_set(CFG, 10 ** 18, state, dry_run=False)  # 1 exabyte
            err = r.get("error", "")
            if "error" not in r or ("headroom" not in err and "refusing" not in err):
                fails.append(f"absurd size didn't refuse on disk headroom: {r!r}")
            if state["in_progress"]:
                fails.append("a refused request left in_progress=True")

            # --- already-in-progress guard (set synchronously, no thread
            # needed to test the guard itself) --------------------------------
            state = _fresh_state()
            state["in_progress"] = True
            r = ramsteind.do_swap_size_set(CFG, 128 * 1024**2, state, dry_run=False)
            if "error" not in r:
                fails.append(f"a concurrent set wasn't refused: {r!r}")

            # --- THE SAFE DEFAULT (alfred's ratification, msg 3429, item 1):
            # omitting dry_run must preview, never touch the backing file or
            # start the worker thread.
            state = _fresh_state()
            r = ramsteind.do_swap_size_set(CFG, size := 128 * 1024 * 1024, state)
            if not r.get("dry_run") or r.get("requested") != size:
                fails.append(f"omitting dry_run did not default to a preview: {r!r}")
            if state["in_progress"] or os.path.exists(BACKING_PATH):
                fails.append("the default (no dry_run arg) call started real work")

            # --- a clean set: real truncate, fake mkswap/systemctl/swapon --
            fakebin = _fakebin(tmp, marker)
            os.environ["PATH"] = fakebin + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_swap_size_set(CFG, size, state, dry_run=False)
            if not r.get("pending"):
                fails.append(f"a valid set didn't report pending: {r!r}")
            result = _wait_done(state)
            if not result or not result.get("ok") or result.get("measured") != size:
                fails.append(f"clean set(size) didn't succeed: {result!r}")
            if not os.path.exists(BACKING_PATH):
                fails.append("set() didn't create the backing file")
            elif os.path.getsize(BACKING_PATH) != size:
                fails.append(
                    f"backing file size wrong: {os.path.getsize(BACKING_PATH)} != {size}")
            status = ramsteind.query_swap_size_status(state)
            if status["active_bytes"] != size:
                fails.append(f"status active_bytes wrong after set: {status!r}")

            # --- remove: real file deletion, fake systemctl disable --------
            r = ramsteind.do_swap_size_remove()
            if not r.get("ok") or r.get("measured") != 0:
                fails.append(f"remove() didn't succeed: {r!r}")
            if os.path.exists(BACKING_PATH):
                fails.append("remove() left the backing file on disk")
            status = ramsteind.query_swap_size_status(state)
            if status["active_bytes"] != 0:
                fails.append(f"status still shows active after remove: {status!r}")

            # --- remove with nothing to remove: clean error, not a crash ---
            r = ramsteind.do_swap_size_remove()
            if r.get("ok") or "error" not in r:
                fails.append(f"remove() with nothing to remove should error: {r!r}")

            # --- NEGATIVE CONTROL 1: mkswap fails -- must never reach
            # enable, must report ok=False naming the real error ------------
            fakebin_fail = _fakebin(tmp, marker, mkswap_fail=True)
            os.environ["PATH"] = fakebin_fail + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_swap_size_set(CFG, size, state, dry_run=False)
            result = _wait_done(state)
            if not result or result.get("ok") or "mkswap" not in (result.get("error") or ""):
                fails.append(f"mkswap failure wasn't surfaced honestly: {result!r}")
            if os.path.exists(marker):
                fails.append("mkswap failed but enable ran anyway (marker written)")
            if os.path.exists(BACKING_PATH):
                os.remove(BACKING_PATH)  # clean up for the next case

            # --- NEGATIVE CONTROL 2 (ruling 41b72476): systemctl enable
            # exits 0 but swapon never actually reflects the new size --
            # the daemon must NOT trust the exit code, must re-measure
            # and report ok=False on the disagreement -------------------
            fakebin_noop = _fakebin(tmp, marker, enable_noop=True)
            os.environ["PATH"] = fakebin_noop + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_swap_size_set(CFG, size, state, dry_run=False)
            result = _wait_done(state)
            if not result or result.get("ok"):
                fails.append(
                    f"a systemctl enable that didn't move swapon's own report"
                    f" was trusted as success: {result!r} (the exact failure"
                    f" class ruling 41b72476 exists to catch)")
    finally:
        os.environ["PATH"] = old_path

    if fails:
        print("SWAP-SIZE TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("swap-size ok: range/headroom/concurrency guards, clean set/remove,"
          " and honest failure on both a failed mkswap and a systemctl"
          " enable that didn't actually move the world")


if __name__ == "__main__":
    main()
