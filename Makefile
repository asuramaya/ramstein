# ramstein — the memory demon
.PHONY: smoke attack install uninstall pill deb check-sutra

VERSION := $(shell tr -d '[:space:]' < VERSION)
DEBROOT := build/deb/ramstein_$(VERSION)_all
DEBFILE := build/deb/ramstein_$(VERSION)_all.deb

smoke: check-sutra
	bash tests/smoke.sh

# Drift guard for every vendored commons file (Wave B, custodian ruling —
# supersedes the old plain-HEAD-compare with the LAG/DRIFT split, kast's
# reference recipe). Integrity (hash matches what vendor.sh recorded — the
# copy wasn't hand-edited) always runs, hard-fail on mismatch, no canonical
# checkout needed. Freshness runs only when ~/code/REPOS/sutra is a git
# checkout, and reads each file's .commit anchor to ask canonical git which
# of three things this is: EXACT (recorded commit == canonical HEAD), LAG
# (recorded commit is an ancestor of HEAD — an old but honest vendor: warn,
# exit 0), or DRIFT (recorded commit isn't in canonical's history at all —
# a corrupted anchor or a rewritten canonical history: hard fail). A file
# with no .commit at all (an older vendor, from before the anchor existed)
# reports freshness unknown rather than failing.
check-sutra:
	@for f in bin/sutra.py bin/sutra_update.py bin/sutra_xen.py \
	          extension/ramstein@asuramaya/pill.js; do \
	    vf="$${f%.py}"; vf="$${vf%.js}.version"; \
	    cf="$${f%.py}"; cf="$${cf%.js}.commit"; \
	    ver=$$(cut -d' ' -f1 "$$vf" 2>/dev/null); \
	    sha=$$(awk '{print $$NF}' "$$vf" 2>/dev/null); \
	    actual=$$(sha256sum "$$f" | cut -d' ' -f1); \
	    if [ "$$sha" != "$$actual" ]; then \
	        echo "check-sutra FAIL: $$f doesn't match $$vf" \
	             "(hand-edited? re-vendor: bash ~/code/REPOS/sutra/vendor.sh bin extension/ramstein@asuramaya)"; \
	        exit 1; \
	    fi; \
	    echo "check-sutra: integrity ok ($$f, $$ver, sha256 $$sha)"; \
	    canon="$$HOME/code/REPOS/sutra"; \
	    if [ -d "$$canon/.git" ]; then \
	        if [ ! -f "$$cf" ]; then \
	            echo "check-sutra: freshness unknown ($$f has no .commit anchor, an older vendor)"; \
	        else \
	            recorded=$$(cat "$$cf"); \
	            head=$$(git -C "$$canon" rev-parse HEAD); \
	            if [ "$$recorded" = "$$head" ]; then \
	                echo "check-sutra: freshness ok ($$f matches canonical HEAD $$head)"; \
	            elif git -C "$$canon" merge-base --is-ancestor "$$recorded" HEAD 2>/dev/null; then \
	                echo "check-sutra: LAG ($$f vendored from $$recorded, canonical has since" \
	                     "moved to $$head) -- warn, not a failure"; \
	            else \
	                echo "check-sutra FAIL: DRIFT ($$f's vendored commit $$recorded is not in" \
	                     "canonical's history at $$canon) -- re-vendor"; \
	                exit 1; \
	            fi; \
	        fi; \
	    else \
	        echo "check-sutra: canonical sutra checkout not present, freshness skipped for $$f"; \
	    fi; \
	done

# the thorough adversarial pass (full cmd surface + oversized/garbage/
# invalid-utf8/nested/unknown/rapid-reconnect/half-open-stall); smoke.sh
# keeps its own quick hostile-input block for a fast loop
attack:
	python3 tests/attack_socket.py

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
	cp -r extension/ramstein@asuramaya $(HOME)/.local/share/gnome-shell/extensions/
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
	install -d -m 0755 $(DEBROOT)/usr/share/man/man1
	install -d -m 0755 $(DEBROOT)/usr/share/man/man8
	install -d -m 0755 $(DEBROOT)/etc/ramstein
	install -d -m 0755 $(DEBROOT)/lib/systemd/system
	install -m 0755 bin/ramsteind bin/ramstein bin/ramstein-healthcheck bin/ramstein-update $(DEBROOT)/usr/bin/
	install -m 0644 bin/sutra.py $(DEBROOT)/usr/bin/sutra.py
	install -m 0644 VERSION $(DEBROOT)/usr/share/ramstein/VERSION
	install -m 0755 scripts/seed-owner-uid.py $(DEBROOT)/usr/share/ramstein/scripts/
	install -m 0644 man/ramstein.1 $(DEBROOT)/usr/share/man/man1/ramstein.1
	install -m 0644 man/ramsteind.8 $(DEBROOT)/usr/share/man/man8/ramsteind.8
	install -m 0644 config/config.json $(DEBROOT)/etc/ramstein/config.json
	install -m 0644 systemd/system/ramsteind.service systemd/system/ramstein-update.service \
	    systemd/system/ramstein-update.timer systemd/system/ramstein-autocalm.service \
	    systemd/system/ramstein-autocalm.timer $(DEBROOT)/lib/systemd/system/
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
	  echo "Depends: python3 (>= 3.8), systemd"; \
	  echo "Maintainer: asuramaya <asuramaya@users.noreply.github.com>"; \
	  echo "Homepage: https://github.com/asuramaya/RAMstein"; \
	  echo "Description: memory as a deadline, not a percentage"; \
	  echo " ramstein owns the truth about bytes alive: /proc+PSI polling, burn"; \
	  echo " rate, ETA-to-OOM, a per-process index, calm/oom/advise, and a GNOME"; \
	  echo " Quick Settings pill."; \
	} > $(DEBROOT)/DEBIAN/control
	dpkg-deb --build --root-owner-group $(DEBROOT) $(DEBFILE)
	@echo "-- built $(DEBFILE)"
	@command -v lintian >/dev/null 2>&1 && lintian $(DEBFILE) || echo "-- lintian not installed, skipping"
