#!/usr/bin/env python3
"""
Tests RAMstein's layer-3 zram verb (decision f8e7cc5a): TOGGLE,
BOUNDED-WAIT, PERSISTENT. Writes /etc/systemd/zram-generator.conf
directly (the one deliberate exception to the drop-in preference --
this file is self-contained and inert with no [zram0] section, see the
daemon's own section-header comment), daemon-reload (re-runs the
generator), start/stop systemd-zram-setup@zram0.service, re-measure via
swapon --show before ever claiming success.

Same marker-file pattern as test_swap_size.py's fake systemctl/swapon
pair: `start` stats nothing (zram devices aren't sized like a plain
file) but writes a fixed marker size, modeling "the device came up";
`stop` clears it. Fulcrum negative controls: the generator package
absent must refuse before ever touching config/systemctl, and a
systemctl start that exits 0 without swapon ever reflecting the device
(the exact "world didn't move" case ruling 41b72476 exists for) must
come back ok=False.

Run as: python3 tests/test_zram.py
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

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-zram-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

DEVICE_MARKER_SIZE = 512 * 1024 * 1024  # fixed, fake -- zram sizing isn't under test here


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def _fakebin(tmp, marker_path, enable_noop=False):
    """systemctl start writes a swapon(8)-shaped marker line (UNLESS
    enable_noop, which exits 0 without writing it -- systemctl
    "succeeding" while the device never actually comes up); stop clears
    it. swapon reads the marker back, same shape as test_swap_size.py."""
    d = tempfile.mkdtemp(dir=tmp)
    start_body = "exit 0" if enable_noop else (
        f'echo "/dev/{ramsteind._ZRAM_DEVICE} file {DEVICE_MARKER_SIZE} 0 -2" > "{marker_path}"\n'
        f'exit 0')
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
if [ "$1" = "daemon-reload" ]; then exit 0; fi
if [ "$1" = "stop" ]; then rm -f "{marker_path}"; exit 0; fi
if [ "$1" = "start" ]; then
{start_body}
fi
exit 1
""")
    _write_exec(os.path.join(d, "swapon"), f"""#!/usr/bin/env bash
[ -f "{marker_path}" ] && cat "{marker_path}"
exit 0
""")
    return d


def _wait_done(zram_state, timeout=10):
    start = time.time()
    while zram_state.get("in_progress") and time.time() - start < timeout:
        time.sleep(0.02)
    return zram_state.get("last_result")


def _fresh_state():
    return {"in_progress": False, "last_result": None}


def main():
    fails = []
    old_path = os.environ.get("PATH", "")
    old_gen = os.environ.get("RAMSTEIN_ZRAM_GENERATOR_BIN")
    old_etc = os.environ.get("RAMSTEIN_ZRAM_ETC_ROOT")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "swapon-marker")
            etc_root = os.path.join(tmp, "etc")
            os.makedirs(etc_root, exist_ok=True)
            os.environ["RAMSTEIN_ZRAM_ETC_ROOT"] = etc_root
            config_path = ramsteind._zram_config_path()

            # --- generator absent: must refuse BEFORE touching config or
            # systemctl at all -----------------------------------------------
            missing_gen = os.path.join(tmp, "no-such-generator")
            os.environ["RAMSTEIN_ZRAM_GENERATOR_BIN"] = missing_gen
            os.environ["PATH"] = _fakebin(tmp, marker) + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state)
            if not r.get("pending"):
                fails.append(f"enable() with generator absent didn't even report pending: {r!r}")
            result = _wait_done(state)
            if not result or result.get("ok") or "not installed" not in (result.get("error") or ""):
                fails.append(f"missing generator wasn't refused honestly: {result!r}")
            if os.path.exists(config_path):
                fails.append("config was written even though the generator is absent")

            # generator present from here on
            present_gen = os.path.join(tmp, "zram-generator")
            _write_exec(present_gen, "#!/usr/bin/env bash\nexit 0\n")
            os.environ["RAMSTEIN_ZRAM_GENERATOR_BIN"] = present_gen

            # --- already-in-progress guard -----------------------------------
            state = _fresh_state()
            state["in_progress"] = True
            r = ramsteind.do_zram_enable(state)
            if "error" not in r:
                fails.append(f"a concurrent enable wasn't refused: {r!r}")

            # --- clean enable: real config write, fake systemctl/swapon ------
            fakebin = _fakebin(tmp, marker)
            os.environ["PATH"] = fakebin + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state)
            if not r.get("pending"):
                fails.append(f"a valid enable didn't report pending: {r!r}")
            result = _wait_done(state)
            if not result or not result.get("ok") or result.get("measured") != DEVICE_MARKER_SIZE:
                fails.append(f"clean enable() didn't succeed: {result!r}")
            if not ramsteind._zram_config_has_section():
                fails.append("enable() didn't write a [zram0] section")
            status = ramsteind.query_zram_status(state)
            if not status["config_enabled"] or status["active_bytes"] != DEVICE_MARKER_SIZE:
                fails.append(f"status wrong after enable: {status!r}")

            # --- disable: config rewritten without [zram0], marker cleared --
            state = _fresh_state()
            r = ramsteind.do_zram_disable(state)
            result = _wait_done(state)
            if not result or not result.get("ok") or result.get("measured") != 0:
                fails.append(f"disable() didn't succeed: {result!r}")
            if ramsteind._zram_config_has_section():
                fails.append("disable() left a [zram0] section in the config")
            status = ramsteind.query_zram_status(state)
            if status["config_enabled"] or status["active_bytes"] != 0:
                fails.append(f"status wrong after disable: {status!r}")

            # --- NEGATIVE CONTROL (ruling 41b72476): systemctl start exits
            # 0 but swapon never reflects the device -- must not be trusted -
            fakebin_noop = _fakebin(tmp, marker, enable_noop=True)
            os.environ["PATH"] = fakebin_noop + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state)
            result = _wait_done(state)
            if not result or result.get("ok"):
                fails.append(
                    f"a systemctl start that didn't move swapon's own report"
                    f" was trusted as success: {result!r} (the exact failure"
                    f" class ruling 41b72476 exists to catch)")
    finally:
        os.environ["PATH"] = old_path
        for var, old in (("RAMSTEIN_ZRAM_GENERATOR_BIN", old_gen),
                         ("RAMSTEIN_ZRAM_ETC_ROOT", old_etc)):
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old

    if fails:
        print("ZRAM TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("zram ok: generator-absence refusal, concurrency guard, clean"
          " enable/disable, and honest failure on a systemctl start that"
          " didn't actually move the world")


if __name__ == "__main__":
    main()
