#!/usr/bin/env python3
"""
Fulcrum-standard negative-control test for ramsteind's
_coexisting_oom_fighter()/_oomd_has_enrolled_cgroups(): systemd-oomd being
`is-active` does not mean it is actually enrolled on any cgroup (found
2026-08-02 -- a real machine at 100% swap, above oomd's own Swap Used
Limit, with both Monitored CGroups sections empty). Builds fake systemctl/
oomctl binaries reproducing both the real unenrolled dump (captured live)
and a real enrolled dump (systemd src/oom/oomd-util.c's own
oomd_dump_swap_cgroup_context format), so the bug case and the fix are
both exercised against realistic output, not guesses.

Run as: python3 tests/test_oom_coexist.py
"""
import importlib.machinery
import importlib.util
import os
import stat
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

# ramsteind has no .py suffix, so spec_from_file_location can't infer a
# loader by extension the way it would for a normal module -- pass one
# explicitly, or spec comes back None.
_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

# Real, live-captured output shape (2026-08-02) -- not invented.
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

# Shape from systemd's own oomd_dump_swap_cgroup_context (src/oom/oomd-util.c):
# "%sPath: %s\n%s\tSwap Usage: %s\n" with prefix="\t" at the manager's own
# dump call site -- i.e. one tab before "Path:", two before "Swap Usage:".
ENROLLED_DUMP = UNENROLLED_DUMP.replace(
    "Swap Monitored CGroups:\n",
    "Swap Monitored CGroups:\n"
    "\tPath: /user.slice/oomd-fixture-test.scope\n"
    "\t\tSwap Usage: 4.0K\n",
)


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def _fakebin(tmp, active_units, oomctl_stdout, no_oomctl=False):
    """active_units: unit names `systemctl is-active` should report
    'active' for. no_oomctl=True omits the oomctl binary entirely (tests
    the OSError/FileNotFoundError path -- oomctl not installed)."""
    d = tempfile.mkdtemp(dir=tmp)
    units = " ".join(f'"{u}"' for u in active_units)
    _write_exec(os.path.join(d, "systemctl"), f"""#!/usr/bin/env bash
if [ "$1" = "is-active" ]; then
  for u in {units}; do
    [ "$2" = "$u" ] && {{ echo active; exit 0; }}
  done
  echo inactive
  exit 3
fi
exit 1
""")
    if not no_oomctl:
        _write_exec(os.path.join(d, "oomctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{oomctl_stdout}
FIXTURE_EOF
""")
    return d


def run_case(tmp, active_units, oomctl_stdout, no_oomctl=False):
    d = _fakebin(tmp, active_units, oomctl_stdout, no_oomctl)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    try:
        return ramsteind._coexisting_oom_fighter()
    finally:
        os.environ["PATH"] = old_path


def main():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        # THE BUG CASE: systemd-oomd active but unenrolled, earlyoom absent.
        # Pre-fix code returns "systemd-oomd" here -- wrong, nothing is
        # actually watching this machine.
        got = run_case(tmp, {"systemd-oomd"}, UNENROLLED_DUMP)
        if got is not None:
            fails.append(f"unenrolled systemd-oomd wrongly reported as fighting: {got!r}")

        # Enrolled: a real Path: line is present -> must be detected.
        got = run_case(tmp, {"systemd-oomd"}, ENROLLED_DUMP)
        if got != "systemd-oomd":
            fails.append(f"enrolled systemd-oomd not detected: {got!r}")

        # Unenrolled oomd + earlyoom active -> falls through to earlyoom,
        # which has no enrollment concept of its own.
        got = run_case(tmp, {"systemd-oomd", "earlyoom"}, UNENROLLED_DUMP)
        if got != "earlyoom":
            fails.append(f"earlyoom fallback broken: {got!r}")

        # Neither active -> None.
        got = run_case(tmp, set(), UNENROLLED_DUMP)
        if got is not None:
            fails.append(f"nothing active but a fighter was reported: {got!r}")

        # oomctl missing entirely (not installed) -> must not crash, must
        # not claim enrollment it can't see.
        got = run_case(tmp, {"systemd-oomd"}, None, no_oomctl=True)
        if got is not None:
            fails.append(f"missing oomctl binary wrongly reported a fighter: {got!r}")

    if fails:
        print("OOM COEXIST TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("oom coexist ok: enrollment checked, not just presence "
          "(unenrolled/enrolled/fallback/none/no-oomctl all correct)")


if __name__ == "__main__":
    main()
