# Security Policy

RAMstein runs a **root daemon** (`ramsteind`) that reads `/proc` and answers a
local Unix socket, so security is taken seriously.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead use
GitHub's private reporting:

1. Go to the repo's **Security** tab → **Report a vulnerability**.
2. Describe the issue, affected version, and a reproduction if possible.

You'll get a response as soon as reasonably possible.

## Threat model

The relevant attacker is an **unprivileged local process** abusing the root
daemon. There is no network attack surface: `ramsteind` never opens a network
socket — its only listener is `AF_UNIX` at `/run/ramstein/control.sock`, and
its only reads are `/proc/meminfo` and `/proc/pressure/memory`.

Hardening in place (see `bin/ramsteind` and `systemd/system/ramsteind.service`):

- **SO_PEERCRED authorization** — only root and the configured `owner_uid`
  may issue commands, checked per-connection on top of the socket's `0660`
  file mode (family ruling 2026-07-14: peercred-gated uid-chown).
- **Hostile-input doctrine** — socket input is hostile by default: bounded
  reads (4 KB line cap), JSON only, exactly two commands; unknown or
  malformed input answers `{"error": ...}` and the connection dies — the
  daemon never crashes on input. The smoke test fuzzes this on every run.
- **Config is the seed, never the master** — every key is typed and clamped
  on load, unknown keys are ignored. A tampered `/etc/ramstein/config.json`
  can tune numbers within clamps; it cannot grant new behaviour or weaken an
  invariant.
- **status.json seam** — written atomically (tmp + rename), mode `0640`,
  chowned to `owner_uid`; readers never see a torn write.
- **Sandboxed unit** — `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome=yes`; writes confined to `/run/ramstein` + `/var/lib/ramstein`.

### The kill invariant (M3, constitution ahead of time)

M0 ships **no kill path at all** — the daemon only observes. When `calm
--kill` lands in M3, its invariant is already law: **nothing is killed
without a fresh, per-invocation confirmation** — no config flag, no
persisting `--yes`, no non-interactive kill path — and a hardcoded exclusion
set (PID 1, kernel threads, ramsteind itself) that config can narrow but
never widen. Any change relaxing this is rejected as a matter of policy, not
review taste.

## Update path

The daemon has no network access; `ramstein-update` is the one component
that does, so it gets its own threat model:

- It runs **unprivileged** (`DynamicUser=yes` in `ramstein-update.service`):
  a version check needs no root.
- The daily timer runs it with **`--check` only** — it checks and logs, it
  never installs unattended (family doctrine: updates are click-to-install).
- In this version the install path is an **explicit stub**: no releases are
  published yet, so there is nothing verifiable to download, and the family
  rule is fail-closed — never install what can't be verified. When releases
  ship, installs inherit the phanspeed model (fail-closed `SHA256SUMS`,
  signature verification once a key is provisioned).
- The API response body is size-capped so a compromised or MITM'd endpoint
  can't balloon the checker's memory.

Adversarial socket tests live inside `tests/smoke.sh` and run on every
`make smoke` and in CI. Please keep them passing in any security-relevant PR.
