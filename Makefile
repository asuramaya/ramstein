# ramstein — the memory demon
.PHONY: smoke attack check check-repo install uninstall pill deb sync-signers

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

# the thorough adversarial pass (full cmd surface + oversized/garbage/
# invalid-utf8/nested/unknown/rapid-reconnect/half-open-stall); smoke.sh
# keeps its own quick hostile-input block for a fast loop
attack:
	python3 tests/attack_socket.py

# static checks, CI-equivalent. Family grammar: smoke attack check deb.
check: check-sutra check-vendored-path-all
	python3 -m py_compile src/bin/ramsteind src/bin/ramstein src/bin/ramstein-healthcheck \
	    src/bin/ramstein-update src/share/ramstein/lib/sutra.py src/share/ramstein/lib/sutra_update.py src/share/ramstein/lib/sutra_xen.py
	node --check "src/extension/ramstein@asuramaya/extension.js" "src/extension/ramstein@asuramaya/pill.js"
	bash -n install.sh uninstall.sh packaging/release-signing/sync-signers.sh tests/smoke.sh
	shellcheck install.sh uninstall.sh packaging/release-signing/sync-signers.sh tests/smoke.sh
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
	install -m 0644 src/data/systemd/system/ramsteind.service src/data/systemd/system/ramstein-update.service \
	    src/data/systemd/system/ramstein-update.timer src/data/systemd/system/ramstein-autocalm.service \
	    src/data/systemd/system/ramstein-autocalm.timer $(DEBROOT)/lib/systemd/system/
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
