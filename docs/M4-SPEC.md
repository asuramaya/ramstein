# RAMstein M4 — completion (spec for till)

Authored by alfred, 2026-07-16. Version 0.5.0 — parity with ByeByte's v1:
after this, RAMstein is Debian-shaped and packaging-complete. ByeByte's
M4 (~/code/REPOS/ByeByte, commits ba2bf88..94f53cf) is the exact pattern —
mirror it, memory dialect. Commit per green milestone on main.

## Scope

Man pages · `make deb` · hardening pass · standalone attack suite.
**The active auto-calm mode stays DEFERRED** — same rule as ByeByte's
Sweep: no automatic memory.high / kill under any name until the operator
names and blesses the mode. eBPF is v2. This milestone is packaging, not
new governance.

## 1. Man pages

`man/ramstein.1` (verbs top/blame/swap/zombies/calm/oom/advise with
examples from real output) and `man/ramsteind.8` (daemon: config keys +
clamps table, /proc + PSI + cgroup data sources, files, signals, security
model: peercred+0660, hostile-input doctrine, the kill gate + oomd
coexistence invariants). groff -man source, `man -l` clean. install.sh +
uninstall.sh place/remove under /usr/local/share/man.

## 2. `make deb`

dpkg-deb package, ByeByte's target as the template: DEBIAN/control
(Package: ramstein, Architecture: all, Depends: python3, systemd), bins,
units + update timer, man pages, conffile /etc/ramstein/config.json,
postinst = the shared config-seed + daemon-reload (factor the shared shell
into scripts/ so install.sh and postinst can't drift — ByeByte did exactly
this). Smoke: build the deb, `dpkg-deb --contents` asserts bins+units+man
present; never install it. lintian if present, notes in your mail.

## 3. Hardening pass

systemd unit: NoNewPrivileges (have it), add SystemCallFilter=@system-service,
ProtectKernelTunables, ProtectClock, MemoryDenyWriteExecute,
RestrictAddressFamilies=AF_UNIX, and the CapabilityBoundingSet RAMstein
actually needs — note that reading other users' /proc + writing cgroup
memory.high + kill/renice drives the cap set; document each retained cap in
a comment. Watch for ByeByte's M4 live-bug lesson: a too-strict sandbox
(theirs was ProtectHome=read-only) silently broke a real action path — the
kill/renice/cgroup writes are your equivalent risk, so verify the hardened
unit against a LIVE action, not just smoke fixtures, before you call it
done. systemd-analyze verify clean.

## 4. Standalone attack suite

Promote the in-smoke hostile block to tests/attack_socket.py (phanspeed +
ByeByte shape) covering the full M2/M3 cmd surface (top/blame/swap/zombies/
calm/oom/advise/kill) plus the classic phases (oversized/garbage/invalid-
utf8/nested/unknown/rapid-reconnect/half-open-stall). After every phase a
normal command must answer. `make attack` first-class; smoke keeps its
quick block. Note: aegis's coldspot fuzzer found 2 real daemon bugs
(wrong-type crash on publish, half-open stall on a single-thread accept
loop) — hunt those exact classes in ramsteind.

## Gate

make smoke + make attack green; deb builds + lists; man renders; py_compile;
healthcheck 0; CHANGELOG `## 0.5.0 — M4 completion`; VERSION + daemon
constant. Mail alfred: smoke+attack tails, dpkg-deb --contents, lintian
notes, any live-found bugs (ByeByte found two at this stage — assume you
will too). v1 declared complete on my review.
