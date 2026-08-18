#!/usr/bin/env python3
"""
Fulcrum-standard test for ramsteind's query_kills(): kernel memcg OOM-kill
events, parsed from journalctl -k output. Built from the 2026-08-18 field
incident (hector-vector's test_agent_access.py OOM-killing a headless
Chrome child 5x) -- the fixture below is real, live-captured journalctl -k
-o short-unix output (two of the five kills), not invented, plus one
synthetic decoy burst to negative-control the pid-correlation logic.

Run as: python3 tests/test_kills.py
"""
import importlib.machinery
import importlib.util
import os
import stat
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

# Real, live-captured (2026-08-18 11:01-11:03, `journalctl -k -o short-unix`
# on the operator's own machine, the incident that motivated this verb) --
# two of the five real kills from that burst, byte-for-byte.
REAL_TWO_KILLS = """\
1787068905.416344 precision kernel: chrome-headless invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=300
1787068905.418264 precision kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=user.slice,mems_allowed=0,oom_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/run-p2663687-i11052929.scope,task_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/run-p2663687-i11052929.scope,task=chrome-headless,pid=2664087,uid=1000
1787068905.418280 precision kernel: Memory cgroup out of memory: Killed process 2664087 (chrome-headless) total-vm:1525450536kB, anon-rss:3258100kB, file-rss:81560kB, shmem-rss:6312kB, UID:1000 pgtables:8808kB oom_score_adj:300
1787068979.287214 precision kernel: Chrome_ChildIOT invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=100
1787068979.289033 precision kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=user.slice,mems_allowed=0,oom_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/run-p2684493-i11077323.scope,task_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/run-p2684493-i11077323.scope,task=chrome-headless,pid=2684673,uid=1000
1787068979.289053 precision kernel: Memory cgroup out of memory: Killed process 2684673 (chrome-headless) total-vm:1525638952kB, anon-rss:3068976kB, file-rss:81840kB, shmem-rss:6404kB, UID:1000 pgtables:8384kB oom_score_adj:300
1787068979.289100 precision systemd[4106]: run-p2684493-i11077323.scope: Consumed 1min 32.072s CPU time over 34.965s wall clock time, 8G memory peak.
"""

# SYNTHETIC decoy: the kernel logs "invoked oom-killer" + the oom-kill:
# accounting line for a candidate pid whose reclaim actually succeeded --
# no matching "Killed process" line ever follows (a real, documented kernel
# shape: oom_reaper can reclaim without killing in some paths). Negative-
# controls _OOM_KILL_LINE_RE's pending_cgroup bookkeeping: a stray entry
# for pid 9999999 must never leak into the result rows, and must not get
# mistakenly popped by a later, unrelated "Killed process" line for a
# different pid.
DECOY_NO_KILL = (
    "1787069000.100000 precision kernel: oom-kill:constraint=CONSTRAINT_MEMCG,"
    "nodemask=(null),cpuset=user.slice,mems_allowed=0,"
    "oom_memcg=/user.slice/user-1000.slice/decoy.scope,"
    "task_memcg=/user.slice/user-1000.slice/decoy.scope,"
    "task=decoy-proc,pid=9999999,uid=1000\n"
)


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def run_case(tmp, journalctl_stdout, returncode=0):
    d = tempfile.mkdtemp(dir=tmp)
    _write_exec(os.path.join(d, "journalctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{journalctl_stdout}
FIXTURE_EOF
exit {returncode}
""")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    try:
        return ramsteind.query_kills(1787068800.0, 20)
    finally:
        os.environ["PATH"] = old_path


def run_no_journalctl_case(tmp):
    d = tempfile.mkdtemp(dir=tmp)  # empty -- journalctl absent from this dir
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d  # nothing else on PATH -- guarantees a miss
    try:
        return ramsteind.query_kills(1787068800.0, 20)
    finally:
        os.environ["PATH"] = old_path


def main():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        # THE REAL INCIDENT, negative-controlled: without this verb, nothing
        # in ramstein could ever answer this query at all (no prior code
        # path -- query_kills is wholly new). The check that matters is
        # that today's actual field data parses into exactly the two real
        # kills, correctly attributed.
        doc = run_case(tmp, REAL_TWO_KILLS)
        rows = doc.get("rows", [])
        if len(rows) != 2:
            fails.append(f"expected 2 kills, got {len(rows)}: {rows!r}")
        else:
            first, second = rows  # newest first
            if first["pid"] != 2684673 or first["comm"] != "chrome-headless":
                fails.append(f"newest kill wrong: {first!r}")
            if first["unit"] != "run-p2684493-i11077323.scope":
                fails.append(f"newest kill's unit wrong: {first!r}")
            if first["rss"] != 3068976 * 1024:
                fails.append(f"newest kill's rss wrong (want anon-rss*1024): {first!r}")
            if second["pid"] != 2664087 or second["cgroup"] != (
                    "/user.slice/user-1000.slice/user@1000.service/app.slice/"
                    "run-p2663687-i11052929.scope"):
                fails.append(f"older kill wrong: {second!r}")

        # DECOY: a pending oom-kill: line with no matching "Killed process"
        # must produce ZERO rows and must not corrupt a real kill's cgroup
        # via wrong pid-keyed lookup.
        doc = run_case(tmp, DECOY_NO_KILL + REAL_TWO_KILLS)
        rows = doc.get("rows", [])
        pids = [r["pid"] for r in rows]
        if 9999999 in pids:
            fails.append(f"decoy pid with no kill line leaked into rows: {rows!r}")
        if len(rows) != 2:
            fails.append(f"decoy burst corrupted real kill count: {len(rows)}")

        # limit must actually truncate.
        d2 = tempfile.mkdtemp(dir=tmp)
        _write_exec(os.path.join(d2, "journalctl"), f"""#!/usr/bin/env bash
cat <<'FIXTURE_EOF'
{REAL_TWO_KILLS}
FIXTURE_EOF
""")
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = d2 + os.pathsep + old_path
        try:
            limited = ramsteind.query_kills(1787068800.0, 1)
        finally:
            os.environ["PATH"] = old_path
        if len(limited.get("rows", [])) != 1:
            fails.append(f"limit=1 not honored: {limited!r}")

        # journalctl genuinely absent -> an honest {"error": ...}, never a
        # silent empty {"rows": []} that reads as "confirmed zero kills"
        # (the same false-negative-vs-unknown distinction practice 2c45d78e
        # names for any control that can fail to observe).
        doc = run_no_journalctl_case(tmp)
        if "error" not in doc:
            fails.append(f"missing journalctl should report error, got: {doc!r}")
        if "rows" in doc:
            fails.append(f"missing journalctl should not also claim rows=[]: {doc!r}")

        # non-zero exit -> same honesty rule.
        doc = run_case(tmp, "", returncode=1)
        if "error" not in doc:
            fails.append(f"journalctl exit!=0 should report error, got: {doc!r}")

    if fails:
        print(f"FAILED ({len(fails)}):")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("test_kills: all cases passed")


if __name__ == "__main__":
    main()
