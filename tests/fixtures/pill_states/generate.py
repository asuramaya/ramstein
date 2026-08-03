#!/usr/bin/env python3
"""
Generates status.json fixtures for every pill state/evidence combination
(ok/warn/hot, singly and combined, plus swap storm) -- built from
ramsteind's OWN classify() and its real DEFAULTS thresholds, not hand-
typed numbers, so a fixture can never silently drift out of sync with
the thresholds that actually produce it. Ready for the headless-
compositor question the moment it opens (alfred, DM #3298/#3301): "the
difference between ready in seconds and ready in twenty minutes is
whether you already have the states on disk to force."

Usage: with the daemon STOPPED (systemctl stop ramsteind), copy a
fixture over the real status.json and the pill's own GFileMonitor
re-renders on the write:

    sudo systemctl stop ramsteind
    sudo cp tests/fixtures/pill_states/hot_multi.json /run/ramstein/status.json
    # look at the tile / open the menu
    sudo systemctl start ramsteind   # restores real polling when done

Regenerate after any change to classify()'s thresholds or poll_memory's
status.json shape: python3 tests/fixtures/pill_states/generate.py
"""
import importlib.machinery
import importlib.util
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RAMSTEIND_PATH = os.path.join(REPO_ROOT, "src", "bin", "ramsteind")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

_loader = importlib.machinery.SourceFileLoader("ramsteind", RAMSTEIND_PATH)
spec = importlib.util.spec_from_file_location("ramsteind", RAMSTEIND_PATH, loader=_loader)
ramsteind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramsteind)

CFG = dict(ramsteind.DEFAULTS)
TOTAL = 64 * 1024**3  # a round 64G box -- easy to eyeball the resulting bytes
SWAP_TOTAL = 8 * 1024**3
TS = 1735000000.0  # fixed, not Date.now() -- reproducible fixtures, no per-run drift


def _mem(avail_pct, some10=None, full10=None, eta_seconds=None,
         swap_free_pct=100.0, burn_bps=0.0, shmem_pct=5.0):
    avail = int(TOTAL * avail_pct / 100.0)
    psi = {"some_avg10": some10, "some_avg60": some10, "some_avg300": some10,
           "full_avg10": full10, "full_avg60": full10, "full_avg300": full10}
    state, evidence = ramsteind.classify(TOTAL, avail, psi, eta_seconds, CFG)
    return {
        "total": TOTAL,
        "available": avail,
        "swap_total": SWAP_TOTAL,
        "swap_free": int(SWAP_TOTAL * swap_free_pct / 100.0),
        "shmem": int(TOTAL * shmem_pct / 100.0),
        "psi": psi,
        "burn_bps": burn_bps,
        "eta_oom_seconds": eta_seconds,
        "state": state,
        "state_evidence": evidence,
    }


def _doc(mem, warning=None, pill=None, autocalm_last=None):
    return {
        "v": 1,
        "ts": TS,
        "daemon": {"version": "0.11.1", "pid": 1, "poll_interval": 10},
        "memory": mem,
        "warning": warning,
        "autocalm": {"enabled": False, "armed": False,
                     "last_action_ts": None, "last_result": autocalm_last},
        "pill": pill or {"top_process": None, "zombie_count": 0,
                          "advise_headline": None, "advise_count": 0},
    }


SCENARIOS = {
    # name: (mem kwargs, extra doc kwargs)
    "ok": (dict(avail_pct=60.0, some10=0.0, full10=0.0, eta_seconds=None), {}),

    "warn_psi": (dict(avail_pct=60.0, some10=15.0, full10=0.0, eta_seconds=None),
                 {"pill": {"top_process": {"pid": 4821, "comm": "chrome", "rss": 2_400_000_000},
                           "zombie_count": 0, "advise_headline": None, "advise_count": 0}}),

    "warn_avail": (dict(avail_pct=10.0, some10=0.0, full10=0.0, eta_seconds=None), {}),

    # eta alone (burn quiet enough that psi/avail both read calm) is the
    # EXACT violation FAMILY.md named: "OOM ~Nh directly above pressure
    # 0.0% / burn quiet" -- this fixture is what the fix looks like now.
    "warn_eta": (dict(avail_pct=60.0, some10=0.0, full10=0.0,
                      eta_seconds=int(3.5 * 3600), burn_bps=1_200_000.0), {}),

    "warn_multi": (dict(avail_pct=10.0, some10=0.0, full10=0.0,
                        eta_seconds=int(2 * 3600), burn_bps=4_000_000.0), {}),

    "hot_psi": (dict(avail_pct=40.0, some10=0.0, full10=8.0, eta_seconds=None), {}),

    "hot_avail": (dict(avail_pct=3.0, some10=12.0, full10=0.0, eta_seconds=None),
                  {"pill": {"top_process": {"pid": 5190, "comm": "python3", "rss": 8_100_000_000},
                            "zombie_count": 2, "advise_headline":
                            "swap 61% full — top tenant python3 (pid 5190, 1073741824B)",
                            "advise_count": 2}}),

    "hot_multi": (dict(avail_pct=3.0, some10=15.0, full10=9.0,
                       eta_seconds=480, burn_bps=45_000_000.0, swap_free_pct=8.0),
                  {"pill": {"top_process": {"pid": 5190, "comm": "python3", "rss": 15_800_000_000},
                            "zombie_count": 4, "advise_headline":
                            "python3 (pid 5190) growing ~900 MiB/h", "advise_count": 3}}),

    # swap storm pre-empts the usual subtitle with its own countdown --
    # independent of classify()'s state, so this one rides on top of a
    # base "ok" memory reading to prove the pre-emption renders correctly
    # even when the underlying state alone wouldn't have said anything.
    "swap_storm": (dict(avail_pct=45.0, some10=2.0, full10=0.0, eta_seconds=None),
                   {"warning": {
                       "kind": "swap_storm",
                       "eta_oom_seconds": 300,
                       "swap_burn_bps": 9_500_000.0,
                       "top_growers": [
                           {"pid": 5190, "comm": "python3", "swap_delta": 620_000_000},
                           {"pid": 4821, "comm": "chrome", "swap_delta": 210_000_000},
                       ],
                   }}),
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    written = []
    for name, (mem_kwargs, doc_kwargs) in SCENARIOS.items():
        mem = _mem(**mem_kwargs)
        doc = _doc(mem, **doc_kwargs)
        path = os.path.join(OUT_DIR, f"{name}.json")
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        written.append((name, mem["state"], mem["state_evidence"]))

    print(f"wrote {len(written)} fixtures to {OUT_DIR}:")
    for name, state, evidence in written:
        print(f"  {name:<12} state={state:<4} evidence={evidence}")


if __name__ == "__main__":
    main()
