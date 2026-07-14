#!/usr/bin/env bash
# Boot the daemon against the real /proc as an unprivileged user,
# assert the status.json shape, poke the socket (including hostile
# input), and exercise the CLI. House tradition: make smoke.
set -euo pipefail
cd "$(dirname "$0")/.."

RD=$(mktemp -d)
trap 'kill "${DPID:-0}" 2>/dev/null || true; rm -rf "$RD"' EXIT

cat > "$RD/config.json" <<EOF
{"poll_interval": 1, "owner_uid": $(id -u)}
EOF

RAMSTEIN_RUNTIME_DIR=$RD python3 bin/ramsteind --config "$RD/config.json" &
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

echo "SMOKE OK"
