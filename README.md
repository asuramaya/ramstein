# ramstein

Bytes alive. The memory sibling of
[byebyte](https://github.com/asuramaya/byebyte) (storage),
[coldspot](https://github.com/asuramaya/coldspot) (internet) and
[phanspeed](https://github.com/asuramaya/phanspeed) (power): a daemon that
owns the truth about your memory, a verb CLI over it, and a GNOME
Quick Settings pill on top.

Where `free` tells you a number, ramstein tells you a *deadline*: available
memory, PSI pressure, burn rate, and an ETA-to-OOM under current pressure —
the pill shows how much is left and how long until the kernel starts
shooting.

```
ramstein status              # available memory, PSI, burn rate, ETA-to-OOM
ramstein top [--swap]        # live per-process RSS + swap, ranked
ramstein blame [--since 10m] # what grew in RSS
ramstein swap                # who's parked in swap
ramstein zombies             # defunct processes and their parent
ramstein oom                 # risk snapshot + who the kernel would kill first
ramstein advise              # nudges: leaks, swap pressure, zombies, ETA
ramstein calm <pid|comm> [--high SIZE|--release|--nice N|--kill]
```

Status: **M3** — the hands (`calm` / `oom` / `advise` live: memory.high /
renice / TTY-confirmed kill, gated by a coexistence check against systemd-
oomd/earlyoom and a daemon-side pid+starttime kill gate), atop M2's per-
process index (sqlite ring over `/proc`, `top` · `blame` · `swap` ·
`zombies`), M1's pill (GNOME Quick Settings: available memory + ETA-to-OOM
on the tile, PSI/swap/burn breakdown expanded; `make pill`, then
`gnome-extensions enable ramstein@asuramaya`), and the M0 truth engine
(/proc/meminfo + /proc/pressure/memory polling, EWMA burn, ETA-to-OOM,
status.json, control socket). See [PLAN.md](PLAN.md) for the road: the
still-unnamed active mode, `.deb`, man pages.

## Install

Two deliberate steps, split by privilege (family doctrine — root installs
the daemon, the pill never needs root):

**Step 1 — the daemon (you type sudo yourself, exactly once):**

```bash
git clone https://github.com/asuramaya/RAMstein
cd RAMstein
sudo ./install.sh        # or: sudo make install
```

Installs `ramstein` / `ramsteind` / `ramstein-healthcheck` /
`ramstein-update` into /usr/local/bin, seeds /etc/ramstein/config.json
(never overwrites yours), wires the systemd units, and starts `ramsteind`.
The daily `ramstein-update.timer` is installed but not enabled, and only
ever *checks* — updates are click-to-install, never unattended.

**Step 2 — the pill (your account, no root):**

```bash
make pill                                    # into YOUR ~/.local — never root
gnome-extensions enable ramstein@asuramaya   # then log out/in once (Wayland)
```

Installing a file into your own home never needed root, so the pill stays
its own per-account step.

Uninstall: `sudo ./uninstall.sh` — keeps /etc/ramstein and /var/lib/ramstein
unless you pass `--purge`.

Free software, GPLv3, stdlib-only Python. No telemetry, no product,
no website — the dream is upstream.
