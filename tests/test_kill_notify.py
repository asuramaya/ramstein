#!/usr/bin/env python3
"""
Tests V3's post-kill desktop notification (alfred's ruling, msg 6500,
2026-09-02) -- ramstein's first unprompted-speech path, built on top of
query_kills (fa65eb2). Ships ON by default: a post-kill notice is a
kernel FACT about the user's own machine after the event, with no
false-positive surface and no action demanded.

Covers:
  - first-ever run bootstraps the high-water mark to "now" WITHOUT
    notifying -- a fresh install must never open with a flood of
    notifications for kills from before ramstein existed on the machine.
  - a genuinely new kill notifies exactly once and advances the
    persisted high-water mark so it is never re-reported.
  - a burst of kills (this session's own hector-vector incident: 5 in
    under 30 minutes) collapses to ONE notification naming the most
    recent, not one popup per kill.
  - the high-water mark persists to disk (STATE_DIR/kill_notify.json)
    and survives a fresh in-process cache -- the disclosed fix for an
    in-memory-only mark resetting on daemon restart.
  - the mark advances even when the notification SEND fails (a kill
    already reported an attempt for is never retried forever) --
    negative-controlled against the alternative (silently retry
    forever), which would be its own kind of noise.
  - notify_kill_enabled=False means check_kill_notifications never even
    calls query_kills, let alone sends anything.

Run as: python3 tests/test_kill_notify.py
"""
import atexit
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")

_STATE_FIXTURE = tempfile.mkdtemp(prefix="ramstein-kill-notify-test-")
atexit.register(shutil.rmtree, _STATE_FIXTURE, ignore_errors=True)
os.environ["RAMSTEIN_STATE_DIR"] = _STATE_FIXTURE

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)


def _kill_row(ts, pid, comm, rss_bytes):
    return {"ts": ts, "pid": pid, "comm": comm, "rss": rss_bytes,
            "cgroup": f"/user.slice/fake-{pid}.scope", "unit": f"fake-{pid}.scope"}


def _reset_state_dir():
    path = ramsteind.KILL_NOTIFY_STATE_PATH
    if os.path.exists(path):
        os.remove(path)


def main():
    fails = []
    cfg = dict(ramsteind.DEFAULTS, notify_kill_enabled=True, owner_uid=1234)

    # 1. FIRST-EVER RUN: no persisted file, no in-process cache -> bootstrap
    # to "now", send NOTHING.
    _reset_state_dir()
    sent = []
    ramsteind.query_kills = lambda since, limit: fails.append(
        "query_kills called on the bootstrap tick -- must not scan history")
    ramsteind._send_desktop_notification = lambda *a: sent.append(a) or True
    state = {}
    ramsteind.check_kill_notifications(cfg, state)
    if sent:
        fails.append(f"bootstrap tick sent a notification: {sent}")
    if "last_ts" not in state:
        fails.append("bootstrap tick didn't populate the in-process cache")
    if not os.path.exists(ramsteind.KILL_NOTIFY_STATE_PATH):
        fails.append("bootstrap tick didn't persist the high-water mark to disk")

    # 2. A GENUINELY NEW KILL notifies exactly once and advances the mark.
    _reset_state_dir()
    base_ts = 1000.0
    with open(ramsteind.KILL_NOTIFY_STATE_PATH, "w") as f:
        json.dump({"last_ts": base_ts}, f)
    sent = []
    new_row = _kill_row(base_ts + 5, 4242, "chrome-headless", 3 * 1024**3)
    ramsteind.query_kills = lambda since, limit: (
        {"rows": [new_row]} if since == base_ts else fails.append(
            f"query_kills called with unexpected since={since}"))
    ramsteind._send_desktop_notification = lambda uid, summary, body: (
        sent.append((uid, summary, body)) or True)
    state = {}
    ramsteind.check_kill_notifications(cfg, state)
    if len(sent) != 1:
        fails.append(f"expected exactly 1 notification, got {len(sent)}: {sent}")
    elif sent[0][0] != 1234:
        fails.append(f"notification sent to the wrong owner_uid: {sent[0]}")
    elif "chrome-headless" not in sent[0][2] or "pid 4242" not in sent[0][2]:
        fails.append(f"notification body missing process identity: {sent[0]}")
    with open(ramsteind.KILL_NOTIFY_STATE_PATH) as f:
        persisted = json.load(f)
    if persisted["last_ts"] != new_row["ts"]:
        fails.append(f"high-water mark didn't advance to the new kill's ts:"
                      f" {persisted}")

    # 3. A SECOND CALL with the SAME state (no new kills since the
    # advanced mark) must NOT re-notify -- the whole point of the mark.
    sent = []
    ramsteind.query_kills = lambda since, limit: {"rows": [new_row]} \
        if since < new_row["ts"] else {"rows": []}
    ramsteind.check_kill_notifications(cfg, state)
    if sent:
        fails.append(f"re-notified a kill already past the high-water mark: {sent}")

    # 4. A BURST (5 kills, this session's own incident shape) collapses to
    # ONE notification naming the MOST RECENT, not 5 popups.
    _reset_state_dir()
    with open(ramsteind.KILL_NOTIFY_STATE_PATH, "w") as f:
        json.dump({"last_ts": base_ts}, f)
    burst = [
        _kill_row(base_ts + 10, 100, "chrome-headless", 1 * 1024**3),
        _kill_row(base_ts + 20, 200, "chrome-headless", 2 * 1024**3),
        _kill_row(base_ts + 30, 300, "chrome-headless", 3 * 1024**3),
        _kill_row(base_ts + 40, 400, "chrome-headless", 4 * 1024**3),
        _kill_row(base_ts + 50, 500, "chrome-headless", 5 * 1024**3),
    ]
    sent = []
    ramsteind.query_kills = lambda since, limit: {"rows": list(reversed(burst))}
    ramsteind._send_desktop_notification = lambda uid, summary, body: (
        sent.append((uid, summary, body)) or True)
    state = {}
    ramsteind.check_kill_notifications(cfg, state)
    if len(sent) != 1:
        fails.append(f"a 5-kill burst should send exactly 1 notification,"
                      f" got {len(sent)}")
    elif "5" not in sent[0][1] or "pid 500" not in sent[0][2]:
        fails.append(f"burst notification doesn't name the count and the"
                      f" MOST RECENT kill: {sent[0]}")

    # 5. PERSISTENCE ROUND-TRIP: a fresh in-process cache (simulating a
    # daemon restart) reads the mark back off disk rather than re-scanning
    # from "now" again.
    _reset_state_dir()
    with open(ramsteind.KILL_NOTIFY_STATE_PATH, "w") as f:
        json.dump({"last_ts": 555.0}, f)
    fresh_state = {}
    seen_since = []
    ramsteind.query_kills = lambda since, limit: seen_since.append(since) or {"rows": []}
    ramsteind.check_kill_notifications(cfg, fresh_state)
    if seen_since != [555.0]:
        fails.append(f"restart didn't read the persisted mark back:"
                      f" queried since={seen_since}, want [555.0]")

    # 6. THE MARK ADVANCES EVEN WHEN THE SEND FAILS -- a kill already
    # reported an attempt for must never be retried forever (that's a
    # DIFFERENT kind of noise than the one this feature exists to add).
    _reset_state_dir()
    with open(ramsteind.KILL_NOTIFY_STATE_PATH, "w") as f:
        json.dump({"last_ts": base_ts}, f)
    failing_row = _kill_row(base_ts + 5, 9999, "postgres", 1 * 1024**3)
    ramsteind.query_kills = lambda since, limit: {"rows": [failing_row]}
    ramsteind._send_desktop_notification = lambda uid, summary, body: False
    state = {}
    ramsteind.check_kill_notifications(cfg, state)
    with open(ramsteind.KILL_NOTIFY_STATE_PATH) as f:
        persisted = json.load(f)
    if persisted["last_ts"] != failing_row["ts"]:
        fails.append("mark did not advance on a failed send -- a failing"
                      " notify-send would retry the same kill forever")

    # 7. DISABLED: never even calls query_kills.
    _reset_state_dir()
    cfg_off = dict(cfg, notify_kill_enabled=False)
    ramsteind.query_kills = lambda since, limit: fails.append(
        "query_kills called while notify_kill_enabled=False")
    ramsteind.check_kill_notifications(cfg_off, {})

    if fails:
        print("KILL NOTIFY TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("kill notify ok: bootstraps silently on first run, notifies exactly"
          " once per genuinely-new kill, collapses a burst to one popup naming"
          " the most recent, persists the high-water mark across a simulated"
          " restart, advances the mark even on a failed send, and stays"
          " fully inert when disabled")


if __name__ == "__main__":
    main()
