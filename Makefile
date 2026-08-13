PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin
SHAREDIR = $(PREFIX)/share/openbox
ICONDIR = $(PREFIX)/share/icons/hicolor/scalable/apps
DESKTOPDIR = $(PREFIX)/share/applications
METAINFODIR = $(PREFIX)/share/metainfo
LICENSEDIR = $(PREFIX)/share/licenses/openbox

PYTHON_SOURCES = $(shell sed '/^[[:space:]]*#/d;/^[[:space:]]*$$/d' runtime_modules.txt)

DATA_FILES = index.html openbox.svg openbox.metainfo.xml LICENSE

.PHONY: install uninstall appimage check version-check dev-venv test-one

dev-venv:
	python3 -m venv .venv-dev
	.venv-dev/bin/pip install ruff coverage

# Full verification gate: lint, compile, tests, coverage floors.
# Dev-only dependencies live in .venv-dev; the runtime app stays dep-free.
check: dev-venv
	python3 scripts/check_tests.py

# A single test file, e.g. `make test-one TEST=test_saves.py`.
test-one:
	python3 -B $(TEST)

# Fails when the version in updates.py disagrees with any published spot.
version-check:
	python3 scripts/check_version_sync.py

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
