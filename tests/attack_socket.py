#!/usr/bin/env python3
"""
Adversarial test harness for ramsteind (phanspeed shape: a fails[] list,
phase-by-phase prints, "ALL ATTACKS DEFENDED" or a SystemExit(1)).

Unlike phanspeed's Daemon, ramsteind has no importable handle_cmd() to fuzz
directly — its dispatch lives inline in Control.handle() against a real
socket. So this harness boots the REAL daemon as a subprocess (same as
make smoke) against an ephemeral fixture, then attacks the socket itself:
every M2/M3 command with hostile field values, plus the classic phases
(oversized/garbage/invalid-utf8/nested/unknown/rapid-reconnect/
half-open-stall). Asserts the daemon never crashes and always answers ping
afterward. Fixture-only, never a real path — calm/kill targets are this
harness's own spawned fixture processes, never anything else on the box.

Run as your normal user:  python3 tests/attack_socket.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []

RD = tempfile.mkdtemp(prefix="ramstein-attack-")
os.makedirs(os.path.join(RD, "fake_cgroup"), exist_ok=True)
os.makedirs(os.path.join(RD, "fakebin"), exist_ok=True)
with open(os.path.join(RD, "fakebin", "systemctl"), "w") as f:
    f.write('#!/usr/bin/env bash\n[ "$1" = "is-active" ] && '
            '{ echo "active"; exit 0; }\nexit 1\n')
os.chmod(os.path.join(RD, "fakebin", "systemctl"), 0o755)
with open(os.path.join(RD, "config.json"), "w") as f:
    json.dump({"poll_interval": 1, "sample_every": 1,
               "proc_min_bytes": 1048576, "owner_uid": os.getuid()}, f)

env = dict(os.environ)
env["PATH"] = os.path.join(RD, "fakebin") + os.pathsep + env.get("PATH", "")
env["RAMSTEIN_RUNTIME_DIR"] = RD
env["RAMSTEIN_STATE_DIR"] = os.path.join(RD, "state")
env["RAMSTEIN_CGROUP_ROOT"] = os.path.join(RD, "fake_cgroup")
proc = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "bin", "ramsteind"),
     "--config", os.path.join(RD, "config.json")],
    env=env)

SOCK = os.path.join(RD, "control.sock")
for _ in range(80):
    if os.path.exists(SOCK):
        break
    time.sleep(0.1)
else:
    print("ramsteind never created its socket")
    raise SystemExit(1)
time.sleep(1.5)  # let at least one M2 sample land


def ask(payload, timeout=8, retries=3):
    """One request/response over a fresh connection. payload: bytes or dict.

    connect()/sendall() are retried on a transient OSError (EAGAIN etc.) —
    the exact race the rapid-reconnect phase deliberately induces: right
    after a connection burst, even a listen() backlog sized for it can
    momentarily refuse a new connect. A real client (bin/ramstein's own
    request()) already tolerates this by treating it as "unreachable, try
    again"; this harness must not mistake a transient hiccup for a crash.
    """
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode() + b"\n"
    for attempt in range(retries):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(SOCK)
            if payload is not None:
                s.sendall(payload)
        except OSError:
            s.close()
            if attempt + 1 == retries:
                return None
            time.sleep(0.05)
            continue
        buf = b""
        try:
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        s.close()
        try:
            return json.loads(buf.decode())
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def alive(where):
    r = ask({"cmd": "ping"})
    if not (isinstance(r, dict) and r.get("ok") is True):
        fails.append(f"[{where}] daemon not answering ping: {r!r}")
        return False
    return True


# a real fixture process, so calm/kill attacks have a live pid+starttime to
# aim hostile field values at without ever touching anything outside this
# harness's own process tree
fixture = subprocess.Popen([sys.executable, "-c",
                            "import time; time.sleep(120)"])
FPID = fixture.pid

# ------------------------------------------------------------- command surface
print("== command-surface hostile fuzz (status/top/blame/swap/zombies/"
      "resolve/oom/advise/calm/kill) ==")
HOSTILE = [
    {"cmd": "status"}, {"cmd": "status", "extra": "garbage"},
    {"cmd": "top"}, {"cmd": "top", "swap": "yes"}, {"cmd": "top", "swap": 1},
    {"cmd": "top", "limit": -1}, {"cmd": "top", "limit": "lots"},
    {"cmd": "top", "limit": 0}, {"cmd": "top", "limit": 99999999},
    {"cmd": "top", "limit": True},
    {"cmd": "blame"}, {"cmd": "blame", "since": "yesterday"},
    {"cmd": "blame", "since": -5}, {"cmd": "blame", "since": None},
    {"cmd": "blame", "since": True}, {"cmd": "blame", "limit": -1},
    {"cmd": "swap"}, {"cmd": "swap", "limit": -1}, {"cmd": "swap", "limit": "x"},
    {"cmd": "zombies"}, {"cmd": "zombies", "extra": [1, 2, 3]},
    {"cmd": "resolve"}, {"cmd": "resolve", "target": 123},
    {"cmd": "resolve", "target": None}, {"cmd": "resolve", "target": ""},
    {"cmd": "resolve", "target": "A" * 5000},
    {"cmd": "oom"}, {"cmd": "oom", "extra": "garbage"},
    {"cmd": "advise"}, {"cmd": "advise", "extra": {"a": 1}},
    {"cmd": "calm"}, {"cmd": "calm", "pid": "123", "starttime": 1,
                       "action": "nice", "nice": 5},
    {"cmd": "calm", "pid": FPID, "starttime": "x", "action": "nice", "nice": 5},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "nice", "nice": -1},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "nice", "nice": 20},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "nice", "nice": "x"},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "high", "size": "-5G"},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "high", "size": "0"},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "high", "size": 123},
    {"cmd": "calm", "pid": FPID, "starttime": 1, "action": "wat"},
    {"cmd": "calm", "pid": 1, "starttime": 1, "action": "nice", "nice": 5},
    {"cmd": "calm", "pid": True, "starttime": 1, "action": "release"},
    {"cmd": "kill"}, {"cmd": "kill", "pid": "123", "starttime": 1, "confirm": "123"},
    {"cmd": "kill", "pid": FPID, "starttime": 1, "confirm": FPID + 1},
    {"cmd": "kill", "pid": FPID, "starttime": 1, "confirm": FPID, "sig": "NUKE"},
    {"cmd": "kill", "pid": 1, "starttime": 1, "confirm": 1},
    {"cmd": "kill", "pid": os.getpid(), "starttime": 1, "confirm": os.getpid()},
    {"cmd": "wat"}, {"cmd": 123}, {"cmd": None}, {}, {"cmd": []},
]
for msg in HOSTILE:
    try:
        r = ask(msg)
        if not isinstance(r, dict):
            fails.append(f"non-dict/garbage response to {msg}: {r!r}")
    except Exception as e:
        fails.append(f"ask() raised on {msg}: {e!r}")
if not alive("command-surface fuzz"):
    pass
print(f"   {len(HOSTILE)} hostile command messages, daemon alive: "
      f"{not any('command-surface' in f or 'ask() raised' in f for f in fails)}")

# real target, wrong identity: pid is real and alive, but starttime/confirm
# are wrong — this must refuse, and the fixture must survive
r = ask({"cmd": "kill", "pid": FPID, "starttime": 1, "confirm": FPID})
if not (isinstance(r, dict) and "error" in r):
    fails.append(f"stale-starttime kill not refused: {r!r}")
try:
    os.kill(FPID, 0)
except OSError:
    fails.append("fixture died despite a refused kill")

# --------------------------------------------------------------- unknown cmd
print("== unknown command ==")
for msg in ({"cmd": "definitely_not_a_real_verb"}, {"cmd": "Top"},
            {"cmd": "TOP"}, {"cmd": "top "}):
    r = ask(msg)
    if not (isinstance(r, dict) and "error" in r):
        fails.append(f"unknown cmd not refused ({msg}): {r!r}")
print(f"   4 unknown-command variants refused, daemon alive: {alive('unknown tail')}")

# ---------------------------------------------------------------- oversized
print("== oversized ==")
big = b'{"cmd":"resolve","target":"' + b"A" * (200 * 1024) + b'"}\n'
r = ask(big)
if not (isinstance(r, dict) and "error" in r):
    fails.append(f"oversized message not refused: {r!r}")
print(f"   200KB payload refused, daemon alive: {alive('oversized tail')}")

# ------------------------------------------------------------------ garbage
print("== garbage / non-object ==")
GARBAGE = (b"not json at all\n", b"[1,2,3]\n", b'"just a string"\n', b"42\n",
           b"null\n", b"true\n", b"\n", b"   \n", b'{"cmd":\n')
for p in GARBAGE:
    r = ask(p)
    if not (isinstance(r, dict) and "error" in r):
        fails.append(f"garbage input not refused ({p!r}): {r!r}")
print(f"   {len(GARBAGE)} garbage payloads refused, daemon alive: "
      f"{alive('garbage tail')}")

# -------------------------------------------------------------- invalid-utf8
print("== invalid utf-8 ==")
# a real client can send arbitrary bytes before any decoding happens; the
# daemon must answer {"error"} (or drop the connection), never crash the
# handler thread on data.decode("utf-8")
INVALID_UTF8 = (b'{"cmd":"resolve","target":"\xff\xfe\x00\x01"}\n',
                 b'\xff\xfe\xfd\xfc\n', b'{"cmd": "top"}\xc0\xaf\n',
                 b"\x00\xff\x02\n")
for p in INVALID_UTF8:
    try:
        r = ask(p)
    except Exception as e:
        fails.append(f"ask() raised on invalid utf-8 {p!r}: {e!r}")
        continue
    # a dropped connection (None) is acceptable; a non-dict reply is not
    if r is not None and not isinstance(r, dict):
        fails.append(f"invalid utf-8 got a non-dict reply: {r!r}")
print(f"   {len(INVALID_UTF8)} invalid-utf8 payloads handled, daemon alive: "
      f"{alive('invalid-utf8 tail')}")

# ------------------------------------------------------------------- nested
print("== nested ==")
# Deep enough to matter but still under MAX_LINE (4096B) so it reaches the
# JSON parser instead of being refused as oversized first.
depth = 1000
nested = (b'{"cmd":"resolve","target":' + b"[" * depth + b"1" + b"]" * depth
          + b'}\n')
assert len(nested) < 4096, "nested payload must stay under MAX_LINE"
r = ask(nested)
if not isinstance(r, dict):
    fails.append(f"nested payload got non-dict/no response: {r!r}")
print(f"   depth-{depth} nested payload handled, daemon alive: "
      f"{alive('nested tail')}")

# ------------------------------------------------------------ rapid-reconnect
print("== rapid reconnect ==")
# aegis's coldspot fuzzer found a half-open stall on a single-thread accept
# loop — ramsteind spawns a handler thread per connection specifically to
# avoid that class. A tight, zero-delay burst can still transiently exceed
# the listen() backlog (normal AF_UNIX behavior, not that bug) — the bar is
# a well-behaved RETRYING client (same as bin/ramstein's own request(), and
# this harness's ask()) always gets through quickly, and the daemon stays
# fully responsive throughout and after. A PERMANENT failure (never
# connects even after retries) is the real signal something's wrong.
N = 200
permanent_failures = 0
for _ in range(N):
    ok = False
    for _attempt in range(5):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(SOCK)
            s.close()
            ok = True
            break
        except OSError:
            s.close()
            time.sleep(0.02)
    if not ok:
        permanent_failures += 1
if permanent_failures:
    fails.append(f"rapid-reconnect: {permanent_failures}/{N} connections "
                 "never succeeded even after retries")
print(f"   {N} rapid connect/disconnect cycles (retried on transient "
      f"refusal), {permanent_failures} permanent failures, daemon alive: "
      f"{alive('rapid-reconnect tail')}")

# --------------------------------------------------------------- half-open-stall
print("== half-open stall (partial message, slow-drip client) ==")
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect(SOCK)
s.sendall(b'{"cmd":"sta')  # deliberately incomplete, no trailing newline
# a half-open connection must never block a fresh, well-behaved one — this
# is the exact bug class aegis found (single-thread accept loop stalling on
# one client); ramsteind's accept loop hands off to a new thread per
# connection specifically so this can't happen
if not alive("during half-open stall"):
    fails.append("a half-open connection blocked a concurrent one")
time.sleep(6)  # past the server's 5s per-connection read timeout
try:
    s.settimeout(2)
    s.recv(4096)
except (socket.timeout, OSError):
    pass
s.close()
if not alive("after half-open stall"):
    fails.append("daemon did not recover after a half-open client")
print(f"   half-open client isolated, daemon alive throughout: "
      f"{alive('half-open-stall tail')}")

# ---------------------------------------------------------------------- done
fixture.terminate()
try:
    fixture.wait(timeout=5)
except subprocess.TimeoutExpired:
    fixture.kill()
    fixture.wait()

proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()

print()
if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL ATTACKS DEFENDED ✔")
