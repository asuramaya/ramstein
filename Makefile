# ramstein — the memory demon
.PHONY: smoke install uninstall pill

smoke:
	bash tests/smoke.sh

# install.sh is root-only and never self-elevates (see its header comment for
# why) — it fails with a clear message if you forget sudo, rather than quietly
# re-invoking itself. So `make install` needs YOU to type sudo, same as the
# script directly: `sudo make install` / `sudo ./install.sh` are equivalent.
install:
	./install.sh

uninstall:
	./uninstall.sh

# the pill only ever needs your own $$HOME and gnome-shell session — never root
pill:
	mkdir -p $(HOME)/.local/share/gnome-shell/extensions
	cp -r extension/ramstein@asuramaya $(HOME)/.local/share/gnome-shell/extensions/
	@echo "pill installed — now: gnome-extensions enable ramstein@asuramaya"
	@echo "then log out and back in once (Wayland reloads extensions at login)"
