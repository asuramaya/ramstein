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


def _fakebin_claimed(tmp, holder_unit):
    """Simulates the real dual-ownership race (alfred, msg 3443, this
    session's own parked EBUSY): `systemctl is-active <holder_unit>`
    reports active -- systemd's OWN auto-generated dev-zram0.swap already
    holding the device, entirely outside this verb's control. Every other
    systemctl verb this test suite exercises (stop/start/daemon-reload)
    must never even be reached once this fires."""
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
if [ "$1" = "is-active" ] && [ "$2" = "{holder_unit}" ]; then
  echo active; exit 0
fi
echo "unexpected systemctl call: $*" >&2
exit 1
""")
    return d


def _fakebin(tmp, marker_path, enable_noop=False):
    """systemctl start writes a swapon(8)-shaped marker line ONLY when the
    marker is currently absent -- real oneshot-unit semantics: `start` on
    an ALREADY-ACTIVE unit is a no-op, it does not re-run ExecStart. This
    is what makes the marker distinguish "stop ran first" (marker
    cleared, so start writes fresh) from "stop was skipped" (a stale
    marker survives start untouched) -- the exact bug found live on the
    operator's machine (DM #3363): a zram device already active from the
    package's own default, `systemctl start` on it a no-op, so a
    size-blind re-measure ("something's active") would have called a
    config change successful when the device never actually moved.
    enable_noop instead exits 0 without EVER writing the marker --
    systemctl "succeeding" while the device never comes up at all; stop
    unconditionally clears it."""
    d = tempfile.mkdtemp(dir=tmp)
    start_body = "exit 0" if enable_noop else (
        f'[ -f "{marker_path}" ] || echo "/dev/{ramsteind._ZRAM_DEVICE} file'
        f' {DEVICE_MARKER_SIZE} 0 -2" > "{marker_path}"\n'
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
            r = ramsteind.do_zram_enable(state, dry_run=False)
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
            r = ramsteind.do_zram_enable(state, dry_run=False)
            if "error" not in r:
                fails.append(f"a concurrent enable wasn't refused: {r!r}")

            # --- clean enable: real config write, fake systemctl/swapon ------
            fakebin = _fakebin(tmp, marker)
            os.environ["PATH"] = fakebin + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state, dry_run=False)
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

            # --- THE SAFE DEFAULT (alfred's ratification, msg 3429, item 1):
            # omitting dry_run must preview, never touch config or state,
            # even with an unclaimed device and the generator present.
            with open(config_path) as f:
                config_before = f.read()
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state)  # no dry_run arg
            if not r.get("dry_run") or "would_configure" not in r:
                fails.append(f"omitting dry_run did not default to a preview: {r!r}")
            if state["in_progress"]:
                fails.append("the default (no dry_run arg) call started the worker")
            with open(config_path) as f:
                if f.read() != config_before:
                    fails.append("the default (no dry_run arg) call rewrote the config")

            # --- PRE-EXISTING DEVICE (DM #3363, live on the operator's
            # machine): a device is already active from something other
            # than RAMstein's own config (the package's own built-in
            # default, or a stale prior size) BEFORE enable() ever runs.
            # enable() must stop it first so the fresh config actually
            # takes effect -- without that, `start` on an already-active
            # oneshot unit is a no-op and the stale size would survive
            # untouched while `measured > 0` still reported ok=True.
            stale_size = DEVICE_MARKER_SIZE * 7  # deliberately NOT what enable() would produce
            with open(marker, "w") as f:
                f.write(f"/dev/{ramsteind._ZRAM_DEVICE} file {stale_size} 0 -2\n")
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state, dry_run=False)
            result = _wait_done(state)
            if not result or not result.get("ok") or result.get("measured") != DEVICE_MARKER_SIZE:
                fails.append(
                    f"enable() over a pre-existing device didn't reconfigure it"
                    f" -- got {result!r}, expected measured={DEVICE_MARKER_SIZE}"
                    f" (stale was {stale_size}): a stop-before-reconfigure"
                    f" regression would leave the stale size in place while"
                    f" still reporting ok=True")

            # --- disable: config rewritten without [zram0], marker cleared --
            state = _fresh_state()
            r = ramsteind.do_zram_disable(state)  # disable is un-gated, no dry_run
            result = _wait_done(state)
            if not result or not result.get("ok") or result.get("measured") != 0:
                fails.append(f"disable() didn't succeed: {result!r}")
            if ramsteind._zram_config_has_section():
                fails.append("disable() left a [zram0] section in the config")
            status = ramsteind.query_zram_status(state)
            if status["config_enabled"] or status["active_bytes"] != 0:
                fails.append(f"status wrong after disable: {status!r}")

            # --- THE DEVICE-CLAIM FLOOR (alfred, msg 3443, correcting an
            # earlier "no parameter, nothing to clamp" framing that
            # answered the wrong question): /dev/zram0 is a raw kernel
            # block device that systemd's OWN dev-zram0.swap can claim
            # independently of this verb -- resetting a device something
            # else already has active as swap pulls backing store out from
            # under a live swap area. Must refuse BEFORE touching config
            # or calling systemctl stop/start at all, on BOTH the preview
            # and the real path, and must name the holding unit. ---------
            claimed_bin = _fakebin_claimed(tmp, ramsteind._ZRAM_DEVICE_SWAP_UNIT)
            os.environ["PATH"] = claimed_bin + os.pathsep + old_path
            holder = ramsteind._zram_device_claimed()
            if holder != ramsteind._ZRAM_DEVICE_SWAP_UNIT:
                fails.append(f"_zram_device_claimed() didn't detect the holder: {holder!r}")

            state = _fresh_state()
            r = ramsteind.do_zram_enable(state)  # dry_run defaults True
            if r.get("dry_run") or "error" not in r or holder not in r["error"]:
                fails.append(
                    f"a claimed device wasn't refused in preview, or didn't"
                    f" name the holder: {r!r}")

            state = _fresh_state()
            r = ramsteind.do_zram_enable(state, dry_run=False)
            result = _wait_done(state)
            if not result or result.get("ok") or holder not in (result.get("error") or ""):
                fails.append(
                    f"a claimed device wasn't refused on the real (--yes)"
                    f" path, or didn't name the holder: {result!r}")

            # NEGATIVE CONTROL: prove the floor is load-bearing, not
            # decorative -- with the check bypassed (simulating the code
            # before this fix), _zram_apply must actually reach systemctl
            # stop against the claimed device. The fake systemctl above
            # only implements is-active; any other call exits 1 with an
            # "unexpected systemctl call" stderr line, so a bypassed check
            # surfaces as a DIFFERENT failure than the clean, named refusal
            # above -- proving the check, not something else, is what
            # produces the refusal.
            real_claimed = ramsteind._zram_device_claimed
            ramsteind._zram_device_claimed = lambda: None
            try:
                state = _fresh_state()
                r = ramsteind.do_zram_enable(state, dry_run=False)
                result = _wait_done(state)
                if result and result.get("ok"):
                    fails.append(
                        "bypassing the floor let a claimed device report"
                        " success -- the fake systemctl should have refused"
                        " the unexpected stop call")
                if result and holder in (result.get("error") or ""):
                    fails.append(
                        "bypassing the floor still produced the named"
                        " refusal -- the negative control isn't testing"
                        " what it claims to (the floor check itself)")
            finally:
                ramsteind._zram_device_claimed = real_claimed

            # --- NEGATIVE CONTROL (ruling 41b72476): systemctl start exits
            # 0 but swapon never reflects the device -- must not be trusted -
            fakebin_noop = _fakebin(tmp, marker, enable_noop=True)
            os.environ["PATH"] = fakebin_noop + os.pathsep + old_path
            state = _fresh_state()
            r = ramsteind.do_zram_enable(state, dry_run=False)
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
