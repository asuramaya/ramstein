# ramstein — the memory demon
.PHONY: smoke attack test check check-repo install uninstall pill deb sync-signers check-systemd-live

VERSION := $(shell tr -d '[:space:]' < packaging/VERSION)
DEBROOT := build/deb/ramstein_$(VERSION)_all
DEBFILE := build/deb/ramstein_$(VERSION)_all.deb

# The family's shared recipe layer (sutra.mk, vendored like code under its
# own .version/.commit anchor — see docs/BOOTSTRAP.md and the file's own
# header). PILL, SUTRA_EXT_DIR and SUTRA_CHECK_BINS must all be set before
# the include; everything else in sutra.mk resolves relative to its own
# vendored location, not this Makefile's.
#
# 0.11.1 folded RAMstein's own pilot supplements upstream (msg 2783):
# SUTRA_EXT_DIR opts check-sutra into also checking pill.js (was a separate
# check-pill-js target here, same integrity+freshness shape, now deleted —
# sutra.mk's own check-sutra covers it). SUTRA_CHECK_BINS is the native
# form of what was a hand-rolled check-vendored-path-all here (four
# `$(MAKE)` calls, one per binary) — same "bin" / "bin:module" shape,
# sutra.mk now owns the loop.
PILL := ramstein
SUTRA_EXT_DIR := src/extension/ramstein@asuramaya
SUTRA_CHECK_BINS := src/bin/ramsteind src/bin/ramstein src/bin/ramstein-healthcheck src/bin/ramstein-update:sutra_update
include src/share/ramstein/lib/sutra.mk

smoke: check-sutra
	bash tests/smoke.sh
	bash tests/test_signing.sh
	# a glob, not a hand list (alfred, msg 3460): the exact defect class
	# `make test` itself exists to close -- a hand list is silently green
	# on a new test file nobody wired in, and the newest two files here
	# (test_zram.py, test_autocalm_arm.py) are precisely the ones a list
	# would have dropped first. Loud on failure by construction (`|| exit
	# 1`), same discipline as the JS-check loop below.
	@for t in tests/test_*.py; do \
	  python3 "$$t" || exit 1; \
	done

# the thorough adversarial pass (full cmd surface + oversized/garbage/
# invalid-utf8/nested/unknown/rapid-reconnect/half-open-stall); smoke.sh
# keeps its own quick hostile-input block for a fast loop
attack:
	python3 tests/attack_socket.py

# THE one true "is it green" (alfred, msg 3456): `python3 -m pytest tests/`
# looks like the obvious thing and lies -- these are standalone scripts
# (their own `tmp` helper, `if __name__ == "__main__": main()`), not
# pytest fixtures, so pytest silently "passes" 6 by accident and errors
# on the other 7 ("fixture 'tmp' not found") while reporting nothing
# broken. `test` is smoke's full test_*.py sweep plus attack_socket.py's
# adversarial pass, run the way they're actually meant to run -- nothing
# else should be trusted as the green/red signal.
test: smoke attack

# static checks, CI-equivalent. Family grammar: smoke attack check deb.
check: check-sutra check-vendored-path-all
	python3 -m py_compile src/bin/ramsteind src/bin/ramstein src/bin/ramstein-healthcheck \
	    src/bin/ramstein-update src/share/ramstein/lib/sutra.py src/share/ramstein/lib/sutra_update.py src/share/ramstein/lib/sutra_xen.py
	# `node --check <path>` on a file with top-level import/export silently
	# skips real syntax validation -- confirmed directly: a file starting
	# with `import Foo from "bar";` followed by an unambiguous syntax error
	# (an unclosed brace) still exits 0. Every extension.js/pill.js in the
	# family is an ES module, always, by construction -- this "syntax"
	# check has been passing malformed GJS since it was written, on every
	# pill that copies this exact line (phanspeed/coldspot/kast all do).
	# `--input-type=module` over stdin parses for real (verified against
	# the same known-bad file: catches it, exit 1) -- found writing new
	# JS for the layer-3 controls and re-checking it, not by auditing this
	# line specifically.
	@for f in "src/extension/ramstein@asuramaya/extension.js" "src/extension/ramstein@asuramaya/pill.js"; do \
	  node --input-type=module --check < "$$f" || exit 1; \
	done
	bash -n install.sh uninstall.sh packaging/release-signing/sync-signers.sh tests/smoke.sh tests/test_signing.sh
	shellcheck install.sh uninstall.sh packaging/release-signing/sync-signers.sh tests/smoke.sh tests/test_signing.sh
	groff -man -Tutf8 -ww src/data/man/man1/ramstein.1 > /dev/null
	groff -t -man -Tutf8 -ww src/data/man/man8/ramsteind.8 > /dev/null
	@echo "all static checks passed"

# rebuild packaging/release-signing/allowed_signers from the canonical keys
# (see docs/RELEASE-SIGNING.md — do NOT run casually; arm ONLY in the same
# act as cutting the first signed release, per the sequencing rule there)
sync-signers:
	bash packaging/release-signing/sync-signers.sh

# install.sh is root-only and never self-elevates (see its header comment for
# why) — it fails with a clear message if you forget sudo, rather than quietly
# re-invoking itself. So `make install` needs YOU to type sudo, same as the
# script directly: `sudo make install` / `sudo ./install.sh` are equivalent.
install:
	./install.sh

uninstall:
	./uninstall.sh

# the pill only ever needs your own $$HOME and gnome-shell session — never root.
# `gnome-extensions disable/enable` (and even the ReloadExtension D-Bus call,
# which recent gnome-shell declares but leaves unimplemented — confirmed by
# hand against 50.1) only re-fire the extension's lifecycle hooks; they don't
# re-import the ES module, so code changes silently keep running the old
# import until the process that holds it dies. On Wayland gnome-shell IS the
# compositor, so that's a log out and back in — there's no in-place restart
# the way X11's Alt+F2 r has one.
pill:
	mkdir -p $(HOME)/.local/share/gnome-shell/extensions
	cp -r src/extension/ramstein@asuramaya $(HOME)/.local/share/gnome-shell/extensions/
	@echo "pill installed — now: gnome-extensions enable ramstein@asuramaya"
	@echo "code changes after that need a log out/in to actually take effect"
	@echo "(disable+enable only re-fires lifecycle hooks, doesn't re-import the JS)"

# Bins land straight in /usr/bin, not /usr/lib/ramstein + symlinks: every
# binary here (ramsteind, ramstein, ramstein-healthcheck, ramstein-update)
# is meant to be run directly by a human or systemd — none is an internal
# helper, so a private libdir + symlink layer would only add indirection
# nothing here needs. Builds only; never installs the result.
deb:
	rm -rf $(DEBROOT)
	install -d -m 0755 $(DEBROOT)/DEBIAN
	install -d -m 0755 $(DEBROOT)/usr/bin
	install -d -m 0755 $(DEBROOT)/usr/share/ramstein/scripts
	install -d -m 0755 $(DEBROOT)/usr/share/ramstein/lib
	install -d -m 0755 $(DEBROOT)/usr/share/man/man1
	install -d -m 0755 $(DEBROOT)/usr/share/man/man8
	install -d -m 0755 $(DEBROOT)/etc/ramstein
	install -d -m 0755 $(DEBROOT)/lib/systemd/system
	install -m 0755 src/bin/ramsteind src/bin/ramstein src/bin/ramstein-healthcheck src/bin/ramstein-update $(DEBROOT)/usr/bin/
	install -m 0644 src/share/ramstein/lib/sutra.py src/share/ramstein/lib/sutra.version src/share/ramstein/lib/sutra.commit \
	    src/share/ramstein/lib/sutra_update.py src/share/ramstein/lib/sutra_update.version src/share/ramstein/lib/sutra_update.commit \
	    src/share/ramstein/lib/sutra_xen.py src/share/ramstein/lib/sutra_xen.version src/share/ramstein/lib/sutra_xen.commit \
	    $(DEBROOT)/usr/share/ramstein/lib/
	install -m 0644 packaging/VERSION $(DEBROOT)/usr/share/ramstein/VERSION
	install -m 0644 packaging/release-signing/allowed_signers $(DEBROOT)/usr/share/ramstein/allowed_signers
	install -m 0755 packaging/seed-owner-uid.py $(DEBROOT)/usr/share/ramstein/scripts/
	install -m 0644 src/data/man/man1/ramstein.1 $(DEBROOT)/usr/share/man/man1/ramstein.1
	install -m 0644 src/data/man/man8/ramsteind.8 $(DEBROOT)/usr/share/man/man8/ramsteind.8
	install -m 0644 src/data/config/config.json $(DEBROOT)/etc/ramstein/config.json
	# rewriting /usr/local/bin -> /usr/bin: the source units hardcode
	# install.sh's source-install prefix, but this target installs binaries
	# under /usr, not /usr/local (two lines up). Shipped verbatim until now
	# (found 2026-08-02, alfred diffing the published v0.11.1 .deb): dpkg
	# succeeds, ExecStart points at a binary the package never installs,
	# the daemon dies 203/EXEC, and postinst's `|| true` swallows it
	# silently. coldspot's build-deb.sh established this exact fix first.
	for u in ramsteind.service ramstein-update.service ramstein-update.timer \
	         ramstein-autocalm.service ramstein-autocalm.timer \
	         var-lib-ramstein-extra.img.swap; do \
	    sed 's#/usr/local/bin#/usr/bin#g' src/data/systemd/system/$$u \
	        > $(DEBROOT)/lib/systemd/system/$$u; \
	done
	install -m 0755 packaging/deb/postinst $(DEBROOT)/DEBIAN/postinst
	install -m 0755 packaging/deb/prerm $(DEBROOT)/DEBIAN/prerm
	install -m 0755 packaging/deb/postrm $(DEBROOT)/DEBIAN/postrm
	echo /etc/ramstein/config.json > $(DEBROOT)/DEBIAN/conffiles
	{ \
	  echo "Package: ramstein"; \
	  echo "Version: $(VERSION)"; \
	  echo "Section: admin"; \
	  echo "Priority: optional"; \
	  echo "Architecture: all"; \
	  echo "Depends: python3 (>= 3.8), systemd, openssh-client"; \
	  echo "Suggests: gnome-shell, systemd-zram-generator"; \
	  echo "Maintainer: asuramaya <asuramaya@users.noreply.github.com>"; \
	  echo "Homepage: https://github.com/asuramaya/RAMstein"; \
	  echo "Description: memory as a deadline, not a percentage"; \
	  echo " ramstein owns the truth about bytes alive: /proc+PSI polling, burn"; \
	  echo " rate, ETA-to-OOM, a per-process index, calm/oom/advise, and a GNOME"; \
	  echo " Quick Settings pill."; \
	} > $(DEBROOT)/DEBIAN/control
	dpkg-deb --build --root-owner-group $(DEBROOT) $(DEBFILE)
	( cd build/deb && sha256sum "$$(basename $(DEBFILE))" > SHA256SUMS )
	@echo "-- built $(DEBFILE)"
	@command -v lintian >/dev/null 2>&1 && lintian $(DEBFILE) || echo "-- lintian not installed, skipping"

# Installs the REAL built .deb under a REAL systemd (root, apt-get
# install, systemctl) and proves every shipped unit actually works -- not
# just that its ExecStart text looks right. Family doctrine (commit 2efd0eb):
# ci.yml invokes Makefile targets, not a copy of the check only CI knows
# about, so `sudo make check-systemd-live` reproduces exactly what CI runs
# in the systemd container harness proven out 2026-08-02 (jrei/systemd-
# ubuntu:24.04, --cgroupns=host — plain --privileged is not enough for
# cgroup v2 nesting). $(DEBFILE), not a glob: a might-match-something-
# stale glob is exactly the kind of "looks right, isn't" this whole check
# exists to root out.
#
# The daemon poll is deliberately scoped, not total coverage -- a joint
# finding with ByeByte's own version of this check, 2026-08-02.
# Individual `systemctl show -p <PROP> --value` calls, never a comma-list:
# `-p A,B,C,D --value` does not preserve the requested order (hit for
# real on this check's own first CI run -- a positional `read` silently
# mis-assigned every field even though the service was healthy the whole
# time). 30s / six 5s samples: the negative control that proved this
# check works measured the actual failure mode -- 4 restarts within 5s, 6
# within 15s, against RestartSec=5 -- so six samples over 30s gives real
# margin around a loop that announces itself in the first five seconds.
# This window catches IMMEDIATE-DEATH failures -- bad ExecStart, seccomp
# SIGSYS, a missing capability -- the classes actually shipped between the
# two pills tonight. It does NOT catch a daemon that runs fine for two
# minutes and dies on its first real poll-loop tick -- different failure,
# different detector, not covered here.
check-systemd-live:
	@test "$$(id -u)" = "0" || { echo "check-systemd-live: needs root (apt-get install, systemctl)" >&2; exit 1; }
	# apt-get, not dpkg -i: the zram verb added a real Depends
	# (systemd-zram-generator), and dpkg -i has no dependency resolver --
	# it would leave the package unconfigured on any machine that doesn't
	# already happen to have that package installed, CI included. Found
	# by running this exact install locally before trusting it, not by
	# reading the Makefile and assuming dpkg -i still covered it.
	apt-get update -qq
	apt-get install -y ./$(DEBFILE)
	@for i in 1 2 3 4 5 6; do \
	  active="$$(systemctl show ramsteind.service -p ActiveState --value)"; \
	  restarts="$$(systemctl show ramsteind.service -p NRestarts --value)"; \
	  echo "t+$$((i * 5))s: active=$$active restarts=$$restarts"; \
	  if [ "$$active" != "active" ] || [ "$$restarts" != "0" ]; then \
	    echo "FAIL: ramsteind.service is '$$active' with $$restarts restart(s)" >&2; \
	    journalctl -u ramsteind.service --no-pager -n 50 >&2; \
	    exit 1; \
	  fi; \
	  sleep 5; \
	done
	@echo "check-systemd-live: ramsteind.service stable, active, 0 restarts, over 30s"
	ramstein status
	ramstein-healthcheck
	@for u in ramstein-update.service ramstein-autocalm.service; do \
	  systemctl start $$u; \
	  result="$$(systemctl show $$u -p Result --value)"; \
	  if [ "$$result" != "success" ]; then \
	    echo "FAIL: $$u did not complete (Result=$$result)" >&2; \
	    journalctl -u $$u --no-pager -n 50 >&2; \
	    echo "-- ramsteind.service's own recent journal (the client's failure may be about what the daemon was doing, not the daemon itself) --" >&2; \
	    journalctl -u ramsteind.service --no-pager -n 80 >&2; \
	    exit 1; \
	  fi; \
	  echo "check-systemd-live: $$u completed (Result=success)"; \
	done
	# Completion-checking the .service units above proves each one runs
	# correctly WHEN TRIGGERED -- it says nothing about whether the paired
	# .timer will ever trigger it. A malformed OnCalendar/OnBootSec value,
	# or a Unit= naming a service that doesn't exist (both timers rely on
	# systemd's default name-matching convention, correct today, silently
	# breakable the day one side gets renamed) produce a timer that loads
	# and enables but never fires -- invisible to everything above. Two
	# checks, because testing this for real (ByeByte's own version, joint
	# finding 2026-08-02) showed they catch DIFFERENT halves of it, not the
	# split originally expected: systemd-analyze verify exits 0 even on a
	# malformed timer value -- confirmed directly, it only ever prints a
	# warning line, never a nonzero exit -- so failure is judged on its
	# OUTPUT being non-empty, not its exit code; and it does NOT catch a
	# Unit= pointing at nothing at all (also confirmed directly -- verify
	# is silent about it). That half is caught by actually starting the
	# timer: systemctl start fails outright on a dangling Unit=, and
	# list-timers only ever shows a timer that both started AND has a real
	# resolved NEXT/LEFT.
	@for t in ramstein-update.timer ramstein-autocalm.timer; do \
	  out="$$(systemd-analyze verify $$t 2>&1)"; \
	  if [ -n "$$out" ]; then \
	    echo "FAIL: systemd-analyze verify found a problem in $$t:" >&2; \
	    echo "$$out" >&2; \
	    exit 1; \
	  fi; \
	  if ! systemctl start $$t; then \
	    echo "FAIL: $$t failed to start -- Unit= likely points at a nonexistent service" >&2; \
	    exit 1; \
	  fi; \
	  line="$$(systemctl list-timers --all --no-legend | grep "$$t" || true)"; \
	  systemctl stop $$t; \
	  if [ -z "$$line" ]; then \
	    echo "FAIL: $$t started but never appeared in list-timers -- schedule did not resolve" >&2; \
	    exit 1; \
	  fi; \
	  echo "check-systemd-live: $$t verified ($$line)"; \
	done

# The family's structural gate (REPO-STANDARD.md §5), mechanical only: it
# cannot judge whether a document is any good, only that the shape it's
# supposed to have is actually there and nothing contradicts it. Copied from
# coldspot, the family's reference implementation of this target, and
# adapted to RAMstein's own file list.
check-repo:
	@fail=0; \
	for f in README.md LICENSE Makefile install.sh uninstall.sh .gitignore .gitattributes \
	         docs/USAGE.md docs/ARCHITECTURE.md docs/RELEASING.md; do \
	    if [ ! -e "$$f" ]; then echo "check-repo FAIL: missing $$f"; fail=1; fi; \
	done; \
	if [ ! -e src/data/man/man1/ramstein.1 ] && ! grep -q 'man1/ramstein.1' docs/ARCHITECTURE.md 2>/dev/null; then \
	    echo "check-repo FAIL: no src/data/man/man1/ramstein.1 and no exemption for it"; fail=1; \
	fi; \
	rows=$(SUTRA_ROOT_ROWS); \
	if [ "$$rows" -gt 12 ]; then \
	    echo "check-repo FAIL: root has $$rows rows, standard caps it at 12"; fail=1; \
	else \
	    echo "check-repo: root row count ok ($$rows)"; \
	fi; \
	if ! grep -q '^## Map' README.md 2>/dev/null; then \
	    echo "check-repo FAIL: README.md has no navigation block (## Map)"; fail=1; \
	fi; \
	for h in Troubleshooting "Repo Layout"; do \
	    if grep -q "^## $$h" README.md 2>/dev/null; then \
	        echo "check-repo FAIL: README.md carries a post-install heading ('$$h') that belongs in docs/USAGE.md"; fail=1; \
	    fi; \
	done; \
	if [ ! -f packaging/VERSION ]; then \
	    echo "check-repo FAIL: no packaging/VERSION"; fail=1; \
	fi; \
	if grep -rn "VERSION[[:space:]]*=[[:space:]]*['\"][0-9]" \
	    src/bin/ramsteind src/bin/ramstein src/bin/ramstein-healthcheck src/bin/ramstein-update \
	    install.sh uninstall.sh packaging/release-signing/sync-signers.sh packaging/seed-owner-uid.py \
	    "src/extension/ramstein@asuramaya/extension.js" 2>/dev/null; then \
	    echo "check-repo FAIL: a literal version string exists outside packaging/VERSION"; fail=1; \
	fi; \
	if grep -v '^[[:space:]]*#' .github/workflows/release.yml 2>/dev/null | grep -q -- '--generate-notes'; then \
	    echo "check-repo FAIL: release.yml still uses --generate-notes, not --notes-file"; fail=1; \
	fi; \
	stray=$$(find docs -name '*.md' -not -path '*/.*' | while read -r f; do git ls-files --error-unmatch "$$f" >/dev/null 2>&1 || echo "$$f"; done); \
	if [ -n "$$stray" ]; then \
	    echo "check-repo FAIL: untracked *.md under docs/: $$stray"; fail=1; \
	fi; \
	spec=$$(find . -name '*-SPEC.md' -not -path './.git/*'); \
	if [ -n "$$spec" ]; then \
	    echo "check-repo FAIL: *-SPEC.md left in the repo (specs belong in the seat's office): $$spec"; fail=1; \
	fi; \
	if [ -f docs/ARCHITECTURE.md ] && grep -q '^## Standard exemptions' docs/ARCHITECTURE.md; then \
	    bad=$$(awk '/^## Standard exemptions/{f=1;next} f && /^\|/ && !/^\| *Item *\|/ && !/^\|---/{ n=gsub(/\|/,"|"); if (n<3) print }' docs/ARCHITECTURE.md); \
	    if [ -n "$$bad" ]; then echo "check-repo FAIL: exemptions table has a row missing a column"; fail=1; fi; \
	fi; \
	if [ "$$fail" -eq 0 ]; then echo "check-repo: all mechanical checks passed"; else exit 1; fi
