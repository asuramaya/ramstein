# ramstein — the memory demon
.PHONY: smoke attack install uninstall pill deb check-sutra

VERSION := $(shell tr -d '[:space:]' < VERSION)
DEBROOT := build/deb/ramstein_$(VERSION)_all
DEBFILE := build/deb/ramstein_$(VERSION)_all.deb

smoke: check-sutra
	bash tests/smoke.sh

# drift guard for the vendored sutra copy: integrity (hash matches what
# vendor.sh recorded — the copy wasn't hand-edited) always runs; freshness
# (diff against the canonical source) only when that checkout is present,
# which it normally isn't in CI.
check-sutra:
	@ver=$$(cut -d' ' -f1 bin/sutra.version); \
	sha=$$(awk '{print $$NF}' bin/sutra.version); \
	actual=$$(sha256sum bin/sutra.py | cut -d' ' -f1); \
	if [ "$$sha" != "$$actual" ]; then \
	    echo "check-sutra FAIL: bin/sutra.py doesn't match bin/sutra.version" \
	         "(hand-edited? re-vendor: bash ~/code/REPOS/sutra/vendor.sh bin)"; \
	    exit 1; \
	fi; \
	echo "check-sutra: integrity ok (sutra $$ver, sha256 $$sha)"; \
	canon="$$HOME/code/REPOS/sutra/sutra.py"; \
	if [ -f "$$canon" ]; then \
	    if cmp -s bin/sutra.py "$$canon"; then \
	        echo "check-sutra: freshness ok (matches canonical)"; \
	    else \
	        echo "check-sutra FAIL: bin/sutra.py differs from canonical $$canon (re-vendor)"; \
	        exit 1; \
	    fi; \
	fi

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
	    systemd/system/ramstein-update.timer $(DEBROOT)/lib/systemd/system/
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
