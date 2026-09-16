PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin
SHAREDIR = $(PREFIX)/share/openbox
ICONDIR = $(PREFIX)/share/icons/hicolor/scalable/apps
DESKTOPDIR = $(PREFIX)/share/applications
METAINFODIR = $(PREFIX)/share/metainfo
LICENSEDIR = $(PREFIX)/share/licenses/openbox

PYTHON_SOURCES = $(shell sed '/^[[:space:]]*#/d;/^[[:space:]]*$$/d' runtime_modules.txt)

DATA_FILES = index.html openbox.svg openbox.metainfo.xml LICENSE assets/openbox-logo.png
STATIC_FILES = $(wildcard static/*.js) $(wildcard static/*.css)
LOCALE_FILES = $(wildcard locales/*.json)
NATIVE_HOST = native_host

.PHONY: install uninstall appimage check check-ci version-check dev-venv test-one native-host shellcheck desktop-appstream flatpak-validate ui-smoke perf

VENV_STAMP = .venv-dev/.requirements.stamp
CI_STRICT ?= 0
STRICT_CASE = if [ "$${OPENBOX_CI_STRICT:-$(CI_STRICT)}" = "1" ]; then echo "check-ci: missing tool in strict mode: $(1)" >&2; exit 1; else echo "check-ci: SKIP $(1) (not installed; OPENBOX_CI_STRICT=1 to fail)"; fi

native-host:
	gcc -O2 native_host.c -o $(NATIVE_HOST) $$(pkg-config --cflags --libs webkit2gtk-4.1)

# Dev-only tooling. The stamp makes this a no-op unless the venv is missing
# or requirements-dev.txt changed, so `make check` no longer reinstalls pip
# dependencies on every run.
dev-venv: $(VENV_STAMP)

$(VENV_STAMP): requirements-dev.txt
	@test -x .venv-dev/bin/python || python3 -m venv .venv-dev
	.venv-dev/bin/pip install --disable-pip-version-check -r requirements-dev.txt
	@touch $(VENV_STAMP)

# Full verification gate: ruff, runtime_modules, v1_contract, version_sync,
# frontend, i18n, csp, docs links, py_compile, api-v2 freshness, tests under
# coverage, coverage floors (total + web_app + changed-line + new-module),
# tokens.
# Dev-only dependencies live in .venv-dev; the runtime app stays dep-free.
check: dev-venv
	python3 scripts/check_tests.py

# Everything CI runs. `check` already covers the gate and the test suite; the
# CI-only jobs are shellcheck, desktop/AppStream validation, the Flatpak
# dry-run, the 10k/20k perf gates, and the puppeteer UI smoke. Locally missing
# tools are reported as SKIP; set OPENBOX_CI_STRICT=1 to fail instead.
check-ci: check shellcheck desktop-appstream flatpak-validate ui-smoke perf

# A single test file, e.g. `make test-one TEST=tests/test_saves.py`.
# Runs against a throwaway OPENBOX_DATA_DIR so a test that skips its own
# isolation cannot touch the developer's real library.
test-one:
	@test -n "$(TEST)" || { echo "usage: make test-one TEST=tests/test_x.py" >&2; exit 2; }
	@d="$$(mktemp -d /tmp/openbox-test-one.XXXXXX)"; \
	trap 'rm -rf "$$d"' EXIT; \
	OPENBOX_DATA_DIR="$$d" python3 -B $(TEST)

shellcheck:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck -S error -x run_all_tests.sh build_appimage.sh openbox.sh openbox-native.sh scripts/*.sh; \
	else \
		$(call STRICT_CASE,shellcheck); \
	fi

desktop-appstream:
	@if command -v desktop-file-validate >/dev/null 2>&1 && command -v appstreamcli >/dev/null 2>&1; then \
		desktop-file-validate openbox.desktop; \
		appstreamcli validate --no-net openbox.metainfo.xml; \
	else \
		$(call STRICT_CASE,desktop-file-validate/appstreamcli); \
	fi

flatpak-validate:
	@if command -v flatpak-builder >/dev/null 2>&1; then \
		flatpak-builder --dry-run --force-clean /tmp/ob-flatpak-dry io.openbox.GameLauncher.yml; \
	else \
		python3 -B scripts/validate_flatpak_manifest.py; \
	fi

ui-smoke:
	@if [ -x scripts/node_modules/.bin/puppeteer ] || [ -d scripts/node_modules/puppeteer ]; then \
		./scripts/ui_smoke.sh; \
	else \
		$(call STRICT_CASE,puppeteer); \
	fi

perf:
	python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5

# Fails when the version in updates.py disagrees with any published spot.
version-check:
	python3 scripts/check_version_sync.py

install: native-host
	install -d $(DESTDIR)$(BINDIR)
	install -d $(DESTDIR)$(SHAREDIR)
	install -d $(DESTDIR)$(SHAREDIR)/themes
	install -d $(DESTDIR)$(ICONDIR)
	install -Dm755 openbox.sh $(DESTDIR)$(BINDIR)/openbox
	install -Dm755 openbox-native.sh $(DESTDIR)$(BINDIR)/openbox-native
	install -d $(DESTDIR)$(DESKTOPDIR)
	install -d $(DESTDIR)$(METAINFODIR)
	install -d $(DESTDIR)$(LICENSEDIR)
	install -Dm755 $(NATIVE_HOST) $(DESTDIR)$(SHAREDIR)/$(NATIVE_HOST)
	install -d $(DESTDIR)$(SHAREDIR)/emulator_defs
	for f in emulator_defs/*.yaml; do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/emulator_defs/"; done
	install -Dm755 scripts/openbox-launcher.sh $(DESTDIR)$(SHAREDIR)/openbox-launcher.sh
	# Runtime Python modules (web_app.py, the handlers/ package, parity_*.py,
	# ...) from runtime_modules.txt, mirroring the repo layout under the share
	# dir. `install -D` creates the handlers/ subdirectory as needed.
	for f in $(PYTHON_SOURCES); do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	# plugin_catalog.py resolves plugins/catalog.json relative to its own path.
	install -Dm644 plugins/catalog.json $(DESTDIR)$(SHAREDIR)/plugins/catalog.json
	for f in $(DATA_FILES); do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	for f in $(STATIC_FILES); do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	for f in $(LOCALE_FILES); do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
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
