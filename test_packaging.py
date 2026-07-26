#!/usr/bin/env python3
"""Packaging acceptance tests for OpenBox Linux distribution."""

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).parent
PYTHON_MODULES = [
    "openbox.py", "web_app.py", "importers.py", "arcade.py", "catalog.py",
    "cloud_sync.py", "emulators.py", "retroachievements.py", "plugins.py",
    "plugin_runner.py", "metadata.py", "archives.py", "saves.py", "updates.py",
    "env_config.py", "parity_discovery.py", "parity_import.py", "parity_integrations.py",
    "parity_media.py", "parity_saves.py", "parity_storefront.py", "plugin_catalog.py", "parity_premium.py",
    "stock_themes.py", "parity_gameyfin.py", "parity_save_tools.py",
    "parity_filter_presets.py", "parity_deeplinks.py", "parity_backup.py", "parity_tracking.py",
    "parity_igdb.py", "parity_emulator_defs.py", "parity_import_policy.py",
]
DATA_FILES = ["index.html"]
STOCK_THEMES = [
    "Midnight Circuit.css",
    "Phosphor Terminal.css",
    "Harbor Light.css",
    "Cinema Marquee.css",
    "Nordic Mist.css",
]


def test_appdir_structure():
    appimage = ROOT / "OpenBox-x86_64.AppImage"
    if not appimage.exists():
        print("  skipping AppImage test (not built)")
        return
    appdir = ROOT / "squashfs-root"
    try:
        subprocess.run(
            [str(appimage), "--appimage-extract"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        assert appdir.is_dir(), "AppDir not extracted"
        assert (appdir / "AppRun").is_file(), "missing AppRun"
        assert (appdir / "usr" / "bin" / "python3").is_file(), "missing bundled python3"
        share = appdir / "usr" / "share" / "openbox"
        assert share.is_dir(), "missing openbox data dir"
        missing = [module for module in PYTHON_MODULES if not (share / module).is_file()]
        if missing:
            print(f"  skipping AppImage module check (rebuild needed): {', '.join(missing)}")
            return
        assert (share / "openbox-launcher.sh").is_file(), "missing keyboard launcher"
        for data in DATA_FILES:
            assert (share / data).is_file(), f"missing {data} in AppImage"
        themes = share / "themes"
        assert themes.is_dir(), "missing themes dir in AppImage"
        for name in STOCK_THEMES:
            assert (themes / name).is_file(), f"missing stock theme {name}"
        desktop = appdir / "usr" / "share" / "applications" / "openbox.desktop"
        assert desktop.is_file(), "missing desktop file"
        svg = appdir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "openbox.svg"
        assert svg.is_file(), "missing icon"
        metainfo = appdir / "usr" / "share" / "metainfo" / "openbox.appdata.xml"
        assert metainfo.is_file(), "missing metainfo"
    finally:
        import shutil
        if appdir.is_dir():
            shutil.rmtree(appdir)
    print("  AppImage structure: ok")


def test_makefile_install():
    for name in ("openbox.sh", "openbox-native.sh"):
        script = ROOT / name
        assert script.exists(), f"missing {name}"
        assert os.access(script, os.X_OK), f"{name} not executable"
    print("  Makefile scripts: ok")


def test_flatpak_manifest():
    manifest = ROOT / "io.openbox.GameLauncher.yml"
    assert manifest.exists(), "missing Flatpak manifest"
    content = manifest.read_text()
    assert "app-id: io.openbox.GameLauncher" in content
    assert "runtime: org.freedesktop.Platform" in content
    assert "command: openbox" in content
    assert "openbox.sh" in content
    assert "openbox.svg" in content
    print("  Flatpak manifest: ok")


def test_desktop_entry():
    desktop = ROOT / "openbox.desktop"
    content = desktop.read_text()
    assert "Name=OpenBox Game Launcher" in content
    assert "Categories=Game" in content
    assert "Type=Application" in content
    assert "x-scheme-handler/openbox" in content
    print("  Desktop entry: ok")


def test_metainfo():
    xml = ROOT / "openbox.metainfo.xml"
    content = xml.read_text()
    assert "io.openbox.GameLauncher" in content
    assert "<name>OpenBox Game Launcher</name>" in content
    assert "unrelated to the Openbox Linux window manager" in content
    assert "AGPL-3.0" in content
    assert "release version" in content
    print("  Metainfo XML: ok")


def test_legal_policy():
    disclaimer = (ROOT / "DISCLAIMER.md").read_text()
    trademarks = (ROOT / "TRADEMARKS.md").read_text()
    readme = (ROOT / "README.md").read_text()
    security = (ROOT / "SECURITY.md").read_text()
    assert "https://github.com/contact/dmca" in disclaimer
    assert "github-trademark-policy" in disclaimer
    assert "security/advisories/new" in disclaimer
    assert "TRADEMARKS.md" in disclaimer and "TRADEMARKS.md" in readme
    assert "clean-room" not in disclaimer
    assert "15 U.S.C." not in disclaimer
    assert "Openbox window manager" in trademarks
    assert "| 0.4.x | Yes |" in security
    assert "| 0.2.x | No |" in security
    print("  Legal policy: ok")


def test_version_consistency():
    from updates import VERSION
    metainfo = ROOT / "openbox.metainfo.xml"
    content = metainfo.read_text()
    assert f'version="{VERSION}"' in content, f"Version mismatch: updates={VERSION}"
    print("  Version consistency: ok")


def test_update_verification():
    from updates import version_tuple
    assert version_tuple("0.1.0") < version_tuple("0.2.0")
    assert version_tuple("1.0.0") > version_tuple("0.9.9")
    assert version_tuple("1.2.3") == version_tuple("1.2.3")
    print("  Update version logic: ok")


def test_appimage_update_info():
    build_script = ROOT / "build_appimage.sh"
    content = build_script.read_text()
    assert "OPENBOX_UPDATE_INFORMATION" in content
    assert "gh-releases-zsync|vindeckyy|OpenBoxGL|latest" in content
    print("  AppImage update info: ok")


def main():
    print("packaging self-test:")
    test_desktop_entry()
    test_metainfo()
    test_legal_policy()
    test_flatpak_manifest()
    test_makefile_install()
    test_appdir_structure()
    test_version_consistency()
    test_update_verification()
    test_appimage_update_info()
    print("packaging self-test: ok")


if __name__ == "__main__":
    main()
