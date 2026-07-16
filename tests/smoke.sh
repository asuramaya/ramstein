#!/usr/bin/env bash
# Boot the daemon against the real /proc as an unprivileged user,
# assert the status.json shape, poke the socket (including hostile
# input), and exercise the CLI. House tradition: make smoke.
set -euo pipefail
cd "$(dirname "$0")/.."

RD=$(mktemp -d)
trap 'kill "${DPID:-0}" "${ZPID:-0}" 2>/dev/null || true; rm -rf "$RD"' EXIT

# sample_every 1 + proc_min_bytes at the clamp floor (1 MiB) so the M2
# fixture processes below show up within a single poll tick
cat > "$RD/config.json" <<EOF
{"poll_interval": 1, "owner_uid": $(id -u), "sample_every": 1,
 "proc_min_bytes": 1048576}
EOF

# M3 fixtures: a fake `systemctl` (always reports oomd active, for the
# coexistence check + advise rule 5) and a fake cgroup root (RAMSTEIN_
# CGROUP_ROOT is only ever honored unprivileged — see bin/ramsteind) so
# `calm --high` has somewhere writable to land instead of the real,
# unwritable /sys/fs/cgroup.
mkdir -p "$RD/fakebin" "$RD/fake_cgroup"
cat > "$RD/fakebin/systemctl" <<'SH'
#!/usr/bin/env bash
[ "$1" = "is-active" ] && { echo "active"; exit 0; }
exit 1
SH
chmod +x "$RD/fakebin/systemctl"

PATH="$RD/fakebin:$PATH" RAMSTEIN_RUNTIME_DIR=$RD RAMSTEIN_STATE_DIR=$RD/state \
    RAMSTEIN_CGROUP_ROOT=$RD/fake_cgroup \
    python3 bin/ramsteind --config "$RD/config.json" &
DPID=$!

for _ in $(seq 1 40); do
    [ -s "$RD/status.json" ] && break
    sleep 0.25
done
[ -s "$RD/status.json" ] || { echo "SMOKE FAIL: no status.json"; exit 1; }

python3 - "$RD/status.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["v"] == 1, "bad version"
assert doc["daemon"]["version"], "no daemon version"
for key in ("pid", "poll_interval"):
    assert key in doc["daemon"], f"daemon missing {key}"
m = doc["memory"]
for key in ("total", "available", "swap_total", "swap_free", "psi",
            "burn_bps", "eta_oom_seconds", "state"):
    assert key in m, f"memory missing {key}"
for key in ("some_avg10", "some_avg60", "some_avg300",
            "full_avg10", "full_avg60", "full_avg300"):
    assert key in m["psi"], f"psi missing {key}"
    v = m["psi"][key]
    assert v is None or isinstance(v, (int, float)), f"psi {key} bad type"
assert m["state"] in ("ok", "warn", "hot"), m["state"]
assert m["total"] > 0, "no total memory"
assert 0 <= m["available"] <= m["total"], "available out of range"
assert 0 <= m["swap_free"] <= m["swap_total"] or m["swap_total"] == 0, \
    "swap_free out of range"
print("shape ok: state", m["state"])
PY

python3 - "$RD/control.sock" <<'PY'
import json, socket, sys

def ask(payload):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(sys.argv[1])
    c.sendall(payload)
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())

assert ask(b'{"cmd":"ping"}\n')["ok"] is True, "ping failed"
assert "memory" in ask(b'{"cmd":"status"}\n'), "status failed"
# hostile input must answer with an error, never crash the daemon
assert "error" in ask(b'not json at all\n'), "garbage not rejected"
assert "error" in ask(b'{"cmd":"rm -rf /"}\n'), "unknown cmd not rejected"
assert ask(b'{"cmd":"ping"}\n')["ok"] is True, "daemon died after abuse"
print("socket ok: ping, status, hostile input survived")
PY

RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein status | grep -q "available" \
    || { echo "SMOKE FAIL: CLI status empty"; exit 1; }
RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein status --json | python3 -c \
    "import json,sys; json.load(sys.stdin)" \
    || { echo "SMOKE FAIL: CLI json invalid"; exit 1; }

# --- M2: the per-process index — top ranks a real allocator, blame sees it
# grow, zombies tracks a real fork lifecycle. Same daemon, same $RD.
python3 - "$RD" <<'PY'
import json, os, socket, sys, time, subprocess

rd = sys.argv[1]
sock_path = os.path.join(rd, "control.sock")


def ask(obj):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(sock_path)
    c.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())


def wait_for_sample_after(min_ts, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        top = ask({"cmd": "top", "limit": 1000})
        if "rows" in top and top.get("ts", 0) > min_ts:
            return top
        time.sleep(0.25)
    raise AssertionError(f"no fresh sample landed within {timeout}s")


t0 = time.time()
wait_for_sample_after(0)  # confirm the sampler is alive before the fixture

# fixture: a ~100MiB allocator that holds the memory and sleeps
child = subprocess.Popen([sys.executable, "-c",
    "import time; buf = bytearray(100 * 1024 * 1024); time.sleep(60)"])
try:
    top = wait_for_sample_after(t0)
    row = next((r for r in top["rows"] if r["pid"] == child.pid), None)
    assert row is not None, (
        f"fixture pid {child.pid} not in top ({len(top['rows'])} rows)")
    assert row["rss"] >= 90 * 1024 * 1024, row
    print(f"top ok: fixture pid {child.pid} ranked with rss {row['rss']}")

    blame = ask({"cmd": "blame", "since": t0, "limit": 1000})
    assert "rows" in blame, blame
    brow = next((r for r in blame["rows"] if r["pid"] == child.pid), None)
    assert brow is not None, f"fixture pid {child.pid} not in blame: {blame}"
    assert brow["delta"] > 0 and brow["new"], brow
    print(f"blame ok: fixture pid {child.pid} delta {brow['delta']} (new)")
finally:
    child.terminate()
    child.wait(timeout=5)

# hostile index input must answer with an error, never crash the daemon
assert "error" in ask({"cmd": "blame", "since": "tuesday"}), "bad since accepted"
assert "error" in ask({"cmd": "top", "limit": 0}), "bad limit accepted"
assert ask({"cmd": "ping"})["ok"] is True, "daemon died after index abuse"
print("hostile M2 input ok: rejected, daemon alive")
PY

# zombie lifecycle: a real fork that exits unreaped, held open by a small
# harness — a plain single fork, not the classic daemonizing double-fork,
# since reparenting to init/a subreaper reaps it too fast to observe.
cat > "$RD/zombie_maker.py" <<'PY'
import os, sys, time
pidfile, reap_flag, done_flag = sys.argv[1:4]
child = os.fork()
if child == 0:
    os._exit(0)
with open(pidfile, "w") as f:
    f.write(str(child))
while not os.path.exists(reap_flag):
    time.sleep(0.1)
os.waitpid(child, 0)
with open(done_flag, "w") as f:
    f.write("reaped")
PY
python3 "$RD/zombie_maker.py" "$RD/zpid" "$RD/reap_now" "$RD/reaped" &
ZPID=$!

for _ in $(seq 1 40); do
    [ -s "$RD/zpid" ] && break
    sleep 0.25
done
[ -s "$RD/zpid" ] || { echo "SMOKE FAIL: zombie fixture never forked"; exit 1; }
ZOMBIE_PID=$(cat "$RD/zpid")

python3 - "$RD" "$ZOMBIE_PID" "$ZPID" <<'PY'
import json, os, socket, sys, time

rd, zpid, zppid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])


def ask(obj):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(os.path.join(rd, "control.sock"))
    c.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())


row = None
for _ in range(40):
    z = ask({"cmd": "zombies"})
    row = next((r for r in z["rows"] if r["pid"] == zpid), None)
    if row:
        break
    time.sleep(0.25)
assert row is not None, f"zombie {zpid} not listed"
assert row["ppid"] == zppid, row
print(f"zombies ok: pid {zpid} listed with ppid {zppid}")
PY

touch "$RD/reap_now"
for _ in $(seq 1 40); do
    [ -s "$RD/reaped" ] && break
    sleep 0.25
done
[ -s "$RD/reaped" ] || { echo "SMOKE FAIL: zombie harness never reaped"; exit 1; }

python3 - "$RD" "$ZOMBIE_PID" <<'PY'
import json, os, socket, sys

rd, zpid = sys.argv[1], int(sys.argv[2])


def ask(obj):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(os.path.join(rd, "control.sock"))
    c.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())


z = ask({"cmd": "zombies"})
assert not any(r["pid"] == zpid for r in z["rows"]), f"zombie {zpid} still listed"
print(f"zombies ok: pid {zpid} cleared after reap")
PY

# M2 CLI verbs end-to-end (human + json output)
RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein top | grep -q "PROC" \
    || { echo "SMOKE FAIL: CLI top empty"; exit 1; }
RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein blame --since 1m --json | python3 -c \
    "import json,sys; json.load(sys.stdin)" \
    || { echo "SMOKE FAIL: CLI blame json invalid"; exit 1; }

# sampler perf canary — one full /proc pass must stay well under budget;
# CI machines are slow, so this is a canary, not a benchmark (M2-SPEC.md)
python3 - <<'PY'
import importlib.util
import time
from importlib.machinery import SourceFileLoader

# bin/ramsteind has no .py suffix, so spec_from_file_location can't infer a
# loader on its own — hand it one explicitly.
loader = SourceFileLoader("ramsteind_perf", "bin/ramsteind")
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

t0 = time.time()
n = len(list(mod._read_procs()))
dt = time.time() - t0
assert dt < 0.5, f"one /proc pass took {dt:.3f}s (budget 0.5s) over {n} procs"
print(f"sampler perf ok: {dt * 1000:.1f}ms for {n} procs")
PY

# --- M3: the hands — calm, oom, advise, and the kill gate ------------------
# oom shape + hostile input, direct socket
python3 - "$RD" <<'PY'
import json, os, socket, sys

rd = sys.argv[1]


def ask(obj):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(os.path.join(rd, "control.sock"))
    c.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())


oom = ask({"cmd": "oom"})
assert "memory" in oom and "candidates" in oom, oom
scores = [c["oom_score"] for c in oom["candidates"]]
assert scores == sorted(scores, reverse=True), f"not sorted by oom_score: {scores}"
for c in oom["candidates"]:
    for key in ("pid", "comm", "rss", "oom_score"):
        assert key in c, f"candidate missing {key}: {c}"
print(f"oom ok: {len(oom['candidates'])} candidates, sorted")

# hostile: kill with a string pid, calm high with a negative size
assert "error" in ask({"cmd": "kill", "pid": "123", "starttime": 1,
                        "confirm": "123"}), "string pid accepted for kill"
assert "error" in ask({"cmd": "calm", "pid": 123456789, "starttime": 1,
                        "action": "high", "size": "-5G"}), \
    "unresolvable pid for high not rejected"
assert ask({"cmd": "ping"})["ok"] is True, "daemon died after M3 hostile input"
print("hostile M3 input ok: rejected, daemon alive")
PY

# advise: a live zombie must fire rule 4, the fake systemctl shim must fire
# rule 5 (coexistence) — reuses zombie_maker.py written during the M2 section
python3 "$RD/zombie_maker.py" "$RD/zpid2" "$RD/reap_now2" "$RD/reaped2" &
ZPID=$!
for _ in $(seq 1 40); do
    [ -s "$RD/zpid2" ] && break
    sleep 0.25
done
[ -s "$RD/zpid2" ] || { echo "SMOKE FAIL: advise zombie fixture never forked"; exit 1; }

python3 - "$RD" <<'PY'
import json, os, socket, sys, time

rd = sys.argv[1]


def ask(obj):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(os.path.join(rd, "control.sock"))
    c.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())


rules = set()
for _ in range(40):
    adv = ask({"cmd": "advise"})
    rules = {line["rule"] for line in adv["lines"]}
    if "zombies" in rules and "coexist" in rules:
        break
    time.sleep(0.25)
assert "zombies" in rules, f"zombie rule never fired: {rules}"
assert "coexist" in rules, f"coexistence rule never fired: {rules}"
print(f"advise ok: rules fired {sorted(rules)}")
PY

touch "$RD/reap_now2"
for _ in $(seq 1 40); do
    [ -s "$RD/reaped2" ] && break
    sleep 0.25
done
[ -s "$RD/reaped2" ] || { echo "SMOKE FAIL: advise zombie harness never reaped"; exit 1; }

# calm: --nice (unprivileged success), --high against the fake cgroup root,
# --release, and the kill gate's refusal paths
python3 - "$RD" <<'PY'
import json, os, socket, subprocess, sys, time

rd = sys.argv[1]
sock_path = os.path.join(rd, "control.sock")


def ask(obj):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(sock_path)
    c.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return json.loads(buf.decode())


child = subprocess.Popen([sys.executable, "-c",
                          "import time; time.sleep(60)"])
try:
    time.sleep(1.5)  # let a sample land so resolve() can see it
    r = ask({"cmd": "resolve", "target": str(child.pid)})
    assert len(r["matches"]) == 1, r
    m = r["matches"][0]

    # --nice: unprivileged, same-uid renice must succeed
    nice_resp = ask({"cmd": "calm", "pid": m["pid"], "starttime": m["starttime"],
                     "action": "nice", "nice": 10})
    assert nice_resp.get("ok") is True, nice_resp
    assert os.getpriority(os.PRIO_PROCESS, child.pid) == 10, "nice not applied"
    print("calm --nice ok: unprivileged renice applied")

    # --high: the daemon writes through RAMSTEIN_CGROUP_ROOT (fake, since
    # this test runs unprivileged and the real cgroup tree isn't writable)
    cg_line = open(f"/proc/{child.pid}/cgroup").read().splitlines()[0]
    rel = cg_line.split(":", 2)[2].lstrip("/")
    fake_dir = os.path.join(rd, "fake_cgroup", rel)
    os.makedirs(fake_dir, exist_ok=True)
    with open(os.path.join(fake_dir, "memory.high"), "w") as f:
        f.write("max")
    high_resp = ask({"cmd": "calm", "pid": m["pid"], "starttime": m["starttime"],
                     "action": "high", "size": "500M"})
    assert high_resp.get("ok") is True, high_resp
    with open(os.path.join(fake_dir, "memory.high")) as f:
        written = f.read().strip()
    assert written == str(500 * 1024 * 1024), \
        f"memory.high got {written}, wanted 500M clamped"
    print("calm --high ok: fake cgroup memory.high written")

    # --release: clears back to max
    rel_resp = ask({"cmd": "calm", "pid": m["pid"], "starttime": m["starttime"],
                    "action": "release"})
    assert rel_resp.get("ok") is True, rel_resp
    with open(os.path.join(fake_dir, "memory.high")) as f:
        assert f.read().strip() == "max", "release didn't clear to max"
    print("calm --release ok: memory.high cleared to max")

    # kill gate: daemon-side pid+starttime mismatch must refuse (the
    # correct AND wrong-confirmation interactive TTY flows are untestable
    # in CI — there is no TTY here; M3-SPEC.md calls this out explicitly)
    stale = ask({"cmd": "kill", "pid": m["pid"], "starttime": 1,
                "confirm": m["pid"], "sig": "TERM"})
    assert "error" in stale, f"stale starttime accepted: {stale}"
    assert ask({"cmd": "ping"})["ok"] is True, "daemon died after kill abuse"
    print("kill gate ok: daemon rejected a stale (pid, starttime) pair")
finally:
    child.terminate()
    child.wait(timeout=5)
PY

# kill gate, CLI side: --kill must refuse outright with no TTY on stdin —
# no --yes, no env bypass, matching the invariant (no non-interactive path)
python3 -c "import time; time.sleep(30)" &
KFIXPID=$!
sleep 1.5
if RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein calm "$KFIXPID" --kill \
        < /dev/null > "$RD/kill_out" 2>&1; then
    echo "SMOKE FAIL: --kill succeeded without a TTY"; cat "$RD/kill_out"; exit 1
fi
grep -qi "tty" "$RD/kill_out" \
    || { echo "SMOKE FAIL: --kill refusal message missing"; cat "$RD/kill_out"; exit 1; }
kill -0 "$KFIXPID" 2>/dev/null \
    || { echo "SMOKE FAIL: fixture died despite refused kill"; exit 1; }
echo "kill gate ok: CLI refused --kill with no TTY, fixture untouched"
kill "$KFIXPID" 2>/dev/null || true

# calm/oom/advise CLI verbs end-to-end (human + json output)
RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein oom | grep -q "who dies first" \
    || { echo "SMOKE FAIL: CLI oom empty"; exit 1; }
RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramstein advise --json | python3 -c \
    "import json,sys; json.load(sys.stdin)" \
    || { echo "SMOKE FAIL: CLI advise json invalid"; exit 1; }

echo "SMOKE OK"
