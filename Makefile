PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin
SHAREDIR = $(PREFIX)/share/openbox
ICONDIR = $(PREFIX)/share/icons/hicolor/scalable/apps
DESKTOPDIR = $(PREFIX)/share/applications
METAINFODIR = $(PREFIX)/share/metainfo
LICENSEDIR = $(PREFIX)/share/licenses/openbox

PYTHON_SOURCES = $(shell sed '/^[[:space:]]*#/d;/^[[:space:]]*$$/d' runtime_modules.txt)

DATA_FILES = index.html openbox.svg openbox.metainfo.xml LICENSE

.PHONY: install uninstall appimage

install:
	install -d $(DESTDIR)$(BINDIR)
	install -d $(DESTDIR)$(SHAREDIR)
	install -d $(DESTDIR)$(SHAREDIR)/themes
	install -d $(DESTDIR)$(ICONDIR)
	install -d $(DESTDIR)$(DESKTOPDIR)
	install -d $(DESTDIR)$(METAINFODIR)
	install -d $(DESTDIR)$(LICENSEDIR)
	install -Dm755 openbox.sh $(DESTDIR)$(BINDIR)/openbox
	install -Dm755 openbox-native.sh $(DESTDIR)$(BINDIR)/openbox-native
	for f in $(PYTHON_SOURCES); do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	install -d $(DESTDIR)$(SHAREDIR)/emulator_defs
	for f in emulator_defs/*.yaml; do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/emulator_defs/"; done
	install -Dm755 scripts/openbox-launcher.sh $(DESTDIR)$(SHAREDIR)/openbox-launcher.sh
	for f in $(DATA_FILES); do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	for f in themes/*.css; do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	install -Dm644 openbox.desktop $(DESTDIR)$(DESKTOPDIR)/io.openbox.GameLauncher.desktop
	install -Dm644 openbox.svg $(DESTDIR)$(ICONDIR)/io.openbox.GameLauncher.svg
	install -Dm644 openbox.metainfo.xml $(DESTDIR)$(METAINFODIR)/io.openbox.GameLauncher.metainfo.xml
	install -Dm644 LICENSE $(DESTDIR)$(LICENSEDIR)/LICENSE

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/openbox
	rm -f $(DESTDIR)$(BINDIR)/openbox-native
	rm -rf $(DESTDIR)$(SHAREDIR)
	rm -f $(DESTDIR)$(ICONDIR)/io.openbox.GameLauncher.svg
	rm -f $(DESTDIR)$(DESKTOPDIR)/io.openbox.GameLauncher.desktop
	rm -f $(DESTDIR)$(METAINFODIR)/io.openbox.GameLauncher.metainfo.xml
	rm -rf $(DESTDIR)$(LICENSEDIR)

appimage:
	bash build_appimage.sh
