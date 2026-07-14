# Contributing to RAMstein

Thanks for your interest! RAMstein is small and dependency-free on purpose —
keep changes simple and self-contained.

## Project layout

```
bin/ramsteind                root daemon (pure Python stdlib, no deps)
bin/ramstein                 verb CLI over the daemon's control socket
bin/ramstein-healthcheck     one-line vitals verdict (status freshness + socket ping)
bin/ramstein-update          GitHub release checker (--check; never installs unattended)
systemd/system/              ramsteind.service + ramstein-update.timer/.service
config/config.json           default config — the seed, never the master
tests/smoke.sh               boots the real daemon, asserts shape + hostile input
install.sh / uninstall.sh
```

## Dev setup

No build step. The smoke test boots the real daemon against the real `/proc`
in a temp runtime dir — no root, no install:

```bash
make smoke                        # must end with "SMOKE OK"
python3 -m py_compile bin/ramsteind bin/ramstein
```

To poke a dev daemon by hand:

```bash
export RAMSTEIN_RUNTIME_DIR=$(mktemp -d)
python3 bin/ramsteind --config config/config.json &
python3 bin/ramstein status
python3 bin/ramstein-healthcheck
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
