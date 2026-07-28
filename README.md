# ramstein

Bytes alive. The memory sibling of
[byebyte](https://github.com/asuramaya/byebyte) (storage),
[coldspot](https://github.com/asuramaya/coldspot) (internet) and
[phanspeed](https://github.com/asuramaya/phanspeed) (power): a daemon that
owns the truth about your memory, a verb CLI over it, and a GNOME
Quick Settings pill on top.

Where `free` tells you a number, ramstein tells you a *deadline*. `free -h`
gives you a snapshot: this many gigabytes available right now. It can't tell
you whether that number is falling, how fast, or when it hits zero. ramstein
watches the trend instead: available memory, PSI pressure, a burn-rate EWMA,
and an ETA-to-OOM under current pressure, so the pill on your screen shows
how much is left and how long until the kernel starts shooting, not a number
that might already be stale by the time you read it.

## Why not just `top` or earlyoom?

`top`/`htop` show memory right now, sorted by RSS, and nothing else: no
trend, no pressure signal, no swap-specific view, no notion of how long
until this becomes a real problem. earlyoom and systemd-oomd solve a
narrower piece of it well (kill something before the kernel thrashes), but
they're reactive by design and tell you nothing until the moment they act.
ramstein sits earlier in that timeline: it watches the same PSI and swap
signals earlyoom does, but surfaces them as a running commentary (`advise`,
the pill, a swap-storm warning) well before anything needs killing, and
offers gentler levers first (`calm --nice`, `calm --high`) that a reactive
killer doesn't have at all. When another OOM-fighter is already active,
ramstein says so and stands down rather than racing it.

```
ramstein status               # available memory, PSI, burn rate, ETA-to-OOM
ramstein calm firefox --nice 10
```

See [docs/USAGE.md](docs/USAGE.md) for the full verb reference, or
`man ramstein`.

## Map

| | |
|---|---|
| Use it | [docs/USAGE.md](docs/USAGE.md), or `man ramstein` / `man 8 ramsteind` |
| Change it | [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) |
| Understand how it's built | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Cut a release | [docs/RELEASING.md](docs/RELEASING.md) |
| See what changed | [docs/CHANGELOG.md](docs/CHANGELOG.md) |
| Report a vulnerability | [.github/SECURITY.md](.github/SECURITY.md) |

## Install

Two deliberate steps, split by privilege (family doctrine: root installs the
daemon, the pill never needs root):

**Step 1, the daemon (you type sudo yourself, exactly once):**

```bash
git clone https://github.com/asuramaya/RAMstein
cd RAMstein
sudo ./install.sh        # or: sudo make install
```

Installs `ramstein` / `ramsteind` / `ramstein-healthcheck` /
`ramstein-update` into /usr/local/bin, seeds /etc/ramstein/config.json
(never overwrites yours), wires the systemd units, and starts `ramsteind`.
The daily `ramstein-update.timer` is installed but not enabled, and only
ever *checks*; updates are click-to-install, never unattended.

**Step 2, the pill (your account, no root):**

```bash
make pill                                    # into YOUR ~/.local, never root
gnome-extensions enable ramstein@asuramaya   # then log out/in once (Wayland)
```

Installing a file into your own home never needed root, so the pill stays
its own per-account step.

Uninstall: `sudo ./uninstall.sh`, keeps /etc/ramstein and /var/lib/ramstein
unless you pass `--purge`.

Free software, GPLv3, stdlib-only Python. No telemetry, no product, no
website; the dream is upstream.
