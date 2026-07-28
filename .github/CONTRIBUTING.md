# Contributing to RAMstein

Thanks for your interest! RAMstein is small and dependency-free on purpose,
keep changes simple and self-contained.

Before changing much, read [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md). It
has the repo map and explains the daemon/CLI/pill split, which is the
decision most likely to bite you if you don't know about it.

## Dev setup

No build step. The smoke test boots the real daemon against the real `/proc`
in a temp runtime dir, no root, no install:

```bash
make smoke                        # must end with "SMOKE OK"
python3 -m py_compile src/bin/ramsteind src/bin/ramstein
```

To poke a dev daemon by hand:

```bash
export RAMSTEIN_RUNTIME_DIR=$(mktemp -d)
python3 src/bin/ramsteind --config src/data/config/config.json &
python3 src/bin/ramstein status
python3 src/bin/ramstein-healthcheck
```

## Before opening a PR

- `make smoke` passes (status.json shape + hostile-input assertions).
- Any new socket/config field is **typed, clamped, and default-safe** in
  `load_config` and the socket handler. The daemon runs as root on a local
  socket — untrusted input must never crash it or weaken an invariant.
- The failsafe invariants are compiled in, never configurable — above all
  the M3 one: **nothing is ever killed without a fresh, per-invocation
  confirmation** (no config flag, no persisting `--yes`, no non-interactive
  kill path). A PR that makes killing configurable will be rejected.
- Keep the daemon dependency-free (Python stdlib only) and networkless —
  the only component that touches the internet is `ramstein-update`.

## License

By contributing you agree your contributions are licensed under
**GPL-3.0-or-later**, matching the project.
