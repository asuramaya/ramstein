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
import shutil
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

# Alfred's review finding (DM #3204): an UNANCHORED "\tPath: " substring
# check fails OPEN -- any future oomctl section that happens to emit a
# tab-indented "Path: " line outside the two real Monitored CGroups
# sections would make it return True. Simulates a hypothetical future
# diagnostic field doing exactly that, BEFORE the real anchor header --
# both monitored sections stay genuinely, actually empty.
SPURIOUS_PATH_DUMP = UNENROLLED_DUMP.replace(
    "System Context:\n",
    "System Context:\n"
    "\tPath: /not/a/real/enrollment (hypothetical future diagnostic field)\n",
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
    if no_oomctl:
        # A missing oomctl must mean genuinely unreachable anywhere on
        # PATH -- omitting it from the fake dir alone isn't enough, since
        # old_path's fallback would still resolve the box's REAL oomctl
        # (this bit a real run: it silently passed only because the real
        # system's oomctl happened to report "unenrolled" too, until an
        # earlier systemd-oomd restart in this same session changed that
        # and exposed the gap). The fake `systemctl` script still needs
        # bash reachable for its own shebang to run -- it uses only bash
        # builtins otherwise -- so symlink just that, nothing else.
        shell_dir = tempfile.mkdtemp(dir=tmp)
        os.symlink(shutil.which("bash"), os.path.join(shell_dir, "bash"))
        new_path = d + os.pathsep + shell_dir
    else:
        new_path = d + os.pathsep + old_path
    os.environ["PATH"] = new_path
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

        # THE FAIL-OPEN CASE (alfred's review, DM #3204): a "Path: " line
        # outside the two real monitored sections must NOT be mistaken for
        # enrollment. An unanchored substring check gets this wrong.
        got = run_case(tmp, {"systemd-oomd"}, SPURIOUS_PATH_DUMP)
        if got is not None:
            fails.append(
                f"a Path: line outside the Monitored CGroups sections wrongly "
                f"counted as enrollment (fails OPEN instead of CLOSED): {got!r}")

    if fails:
        print("OOM COEXIST TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("oom coexist ok: enrollment checked, not just presence "
          "(unenrolled/enrolled/fallback/none/no-oomctl all correct)")


if __name__ == "__main__":
    main()
