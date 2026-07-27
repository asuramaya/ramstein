# Using RAMstein

Everything the CLI, the daemon, and the GNOME pill can do. If you just installed RAMstein, run
`ramstein status` first. For the short version, see the [README](../README.md).

## Everyday commands

```
ramstein status              # available memory, PSI, burn rate, ETA-to-OOM
ramstein top [--swap]        # live per-process RSS + swap, ranked
ramstein blame [--since 10m] # what grew in RSS
ramstein swap                # who's parked in swap
ramstein zombies             # defunct processes and their parent
ramstein oom                 # risk snapshot + who the kernel would kill first
ramstein advise               # nudges: leaks, swap pressure, zombies, ETA
```

## Calming a process down

```
ramstein calm <pid|comm> --nice 10       # renice, the gentlest lever
ramstein calm <pid|comm> --high 500M     # cap its cgroup memory.high
ramstein calm <pid|comm> --release       # lift the cap back to max
ramstein calm <pid|comm> --kill          # the one ungentle lever
```

`--kill` asks you to type the target's pid back, fresh, every single time. There is no `--yes`
and no way to script around it: a non-interactive shell gets refused outright. An ambiguous comm
name (more than one matching process, routinely true for chrome/postgres/python) refuses with
the candidate list; pass a pid instead.

## Auto-calm

Auto-calm lets `calm` act on its own, on a timer, when memory pressure or an active swap-storm
warning crosses your configured thresholds. It never kills; the graduated response stops at a
renice and a cgroup squeeze, and even fully armed it only ever notifies you of the one `calm
--kill` command it will not run itself.

Three gates, all required before a cycle does anything real:

```
ramstein autocalm status     # current enabled/armed state, thresholds, last cycle
ramstein autocalm arm        # let the next triggered cycle act for real (TTY confirm required)
ramstein autocalm dry        # back to disarmed; cycles keep computing and notifying only
ramstein autocalm run        # run one check-and-maybe-act cycle right now
```

Arming resets to disarmed on every daemon restart, and the `ramstein-autocalm.timer` unit ships
installed but not enabled, so you flip on the timer yourself once you want the whole loop live:
`sudo systemctl enable --now ramstein-autocalm.timer`.

## The GNOME pill

`make pill` (as yourself, no sudo) installs the Quick Settings tile: available memory and
ETA-to-OOM on the collapsed tile, heating on a PSI spike or an observed OOM kill; expanded, a PSI
breakdown, the top RSS process, a zombie count, and a swap-storm banner when one is active.
Wayland cannot hot-reload a running shell's extension JS (`Extensions.ReloadExtension` is
declared but not implemented as of GNOME Shell 50.1), so a fresh install or update needs a
logout/login to actually show.

## Updating

```
ramstein update [--check]    # pull a newer release now (manual; signature-verified once armed)
```

The daily `ramstein-update.timer` only ever passes `--check`; it never installs anything
unattended. See [RELEASE-SIGNING.md](RELEASE-SIGNING.md) for what "signature-verified" actually
checks, and where RAMstein currently stands on that chain.

## Troubleshooting

**`ramstein` can't reach the daemon.** Check `systemctl status ramsteind`; the control socket is
`/run/ramstein/control.sock`, mode 0660, and only root or the configured `owner_uid` may issue
commands. `ramstein-healthcheck` gives a one-line verdict (status.json freshness plus a socket
ping) without needing root.

**The pill doesn't show the latest layout after an update.** See the GNOME pill note above: log
out and back in once. There's no in-place Wayland shell restart to fall back on.

**`calm --high` or auto-calm's squeeze step silently does nothing.** The daemon needs a writable
cgroup v2 `memory.high` path for the target; a target outside any cgroup, or one whose cgroup the
hardened systemd unit hasn't been given `ReadWritePaths` to, can't be squeezed. `advise` and
`calm`'s own output say so when a write is refused.
