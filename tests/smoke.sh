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

RAMSTEIN_RUNTIME_DIR=$RD RAMSTEIN_STATE_DIR=$RD/state \
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

echo "SMOKE OK"
