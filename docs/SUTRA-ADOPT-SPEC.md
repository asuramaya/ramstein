# Adopt the sutra backbone (spec for till — RAMstein)

Authored by alfred, 2026-07-17. Twin of ByeByte's pilot spec
(~/code/REPOS/ByeByte/docs/SUTRA-ADOPT-SPEC.md — read it, and read sutra's
tests/toy_daemon.py, the reference). RAMstein is a uid-model daemon, same as
ByeByte — the rewire is the same shape. **Behavior-preserving**: same socket
contract, same status.json, same config. Gate: `make smoke` + `make attack`
stay green and unchanged. Do NOT touch domain logic (the index/sampler, top/
blame/swap/zombies/resolve/oom/advise/calm/kill) — only the shared skeleton.

## Steps

1. **Vendor:** `bash ~/code/REPOS/sutra/vendor.sh bin` → bin/sutra.py +
   bin/sutra.version (commit both; never hand-edit the copy — re-vendor).
   `import sutra` as a sibling in both ramsteind and ramstein.

2. **load_config:** body becomes `cfg = sutra.load_config(path, DEFAULTS,
   CLAMPS)` — RAMstein's config is all numeric/int, no str_patterns needed.
   Delete the old clamp loop.

3. **write_status:** body becomes `sutra.write_status(STATUS_PATH, doc,
   owner=(cfg["owner_uid"], -1))`; keep the `(doc, cfg)` signature.

4. **The EWMA in poll_memory — the one subtle bit.** sutra.ewma_rate takes the
   quantity whose INCREASE is the burn. RAMstein measures memory *consumption*,
   so pass the used-equivalent: `value = total - avail`. Replace the inline
   alpha/exp block with:
   `state["mem"], burn = sutra.ewma_rate(prev, total - avail, now, cfg["burn_tau"])`
   The burn stays positive-when-memory-is-eaten, identical to today; ETA math
   (avail + swap_free) / burn is unchanged. Nothing else reads state["mem"]'s
   fields, so sutra's {t, v, ewma} dict drops in.

5. **The Control class:** DELETE it. In main(), build a `dispatch(cmd, req)`
   closure over cfg/get_status carrying your existing elif bodies for the
   domain commands (top/swap/blame/zombies/resolve/oom/advise/calm/kill),
   returning `None` for unknown — ping/status are sutra's, drop them. Keep the
   `_limit` validator and the NICE_MIN/NICE_MAX guards inside dispatch. Then:
   ```
   ctl = sutra.ControlServer(SOCKET_PATH, sutra.allow_uids({0, uid}), VERSION,
                             dispatch, get_status, socket_owner=(cfg["owner_uid"], -1))
   ctl.start()
   ```
   Drop the now-unused `import struct`. Note the kill gate: sutra validates the
   peer and frames errors, but do_kill's (pid, starttime, confirm) identity
   check and the TTY-confirm live in YOUR dispatch/CLI, unchanged — sutra
   doesn't know about kill semantics.

6. **ramstein (CLI):** `import sutra`; replace the local request() body with
   `sutra.request(os.path.join(RUNTIME_DIR, "control.sock"), payload)` and the
   status fallback with `sutra.read_status(...)`.

7. **Drift guard:** Makefile `check-sutra` target (sha256 bin/sutra.py ==
   bin/sutra.version; cmp against ~/code/REPOS/sutra/sutra.py when present),
   wired into CI + a `make check` aggregate.

## Gate

`make smoke` + `make attack` green + behaviorally unchanged; `make check-sutra`
green; py_compile both bins; VERSION + daemon constant 0.4.1 → 0.5.0 (this is
the M4 packaging milestone's sibling — fold the sutra adoption into your M4 work
or ship it as its own 0.4.2, your call, but keep it a clean commit
`refactor: adopt sutra backbone`). CHANGELOG entry. Mail alfred the smoke+attack
tails + the ramsteind line-count delta (skeleton deleted). Do NOT commit sutra
itself — operator's signed hand, later.
