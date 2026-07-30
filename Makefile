PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin
SHAREDIR = $(PREFIX)/share/openbox
ICONDIR = $(PREFIX)/share/icons/hicolor/scalable/apps
DESKTOPDIR = $(PREFIX)/share/applications
METAINFODIR = $(PREFIX)/share/metainfo
LICENSEDIR = $(PREFIX)/share/licenses/openbox

PYTHON_SOURCES = openbox.py web_app.py openbox_logging.py importers.py arcade.py catalog.py cloud_sync.py \
	emulators.py retroachievements.py plugins.py plugin_runner.py metadata.py \
	archives.py saves.py updates.py env_config.py parity_discovery.py parity_import.py \
	parity_integrations.py parity_media.py parity_saves.py parity_storefront.py plugin_catalog.py parity_premium.py stock_themes.py parity_gameyfin.py parity_save_tools.py \
	parity_filter_presets.py parity_deeplinks.py parity_backup.py parity_tracking.py parity_igdb.py parity_emulator_defs.py parity_import_policy.py parity_gamescope.py

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
