#!/usr/bin/env python3
"""Packaging acceptance tests for OpenBox Linux distribution."""

import os
import ast
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).parent
PYTHON_MODULES = [line.strip() for line in (ROOT / "runtime_modules.txt").read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
DATA_FILES = ["index.html"]
STOCK_THEMES = [
    "Midnight Circuit.css",
    "Phosphor Terminal.css",
    "Harbor Light.css",
    "Cinema Marquee.css",
    "Nordic Mist.css",
]


def test_appdir_structure():
    appimage_path = os.environ.get("OPENBOX_APPIMAGE")
    appimage = Path(appimage_path or ROOT / "OpenBox-x86_64.AppImage").expanduser()
    if not appimage.exists():
        if appimage_path or os.environ.get("OPENBOX_REQUIRE_ARTIFACTS"):
            raise AssertionError(f"AppImage artifact not found: {appimage}")
        print("  skipping AppImage test (not built)")
        return
    if not appimage_path:
        source_mtime = max((ROOT / module).stat().st_mtime for module in PYTHON_MODULES)
        if appimage.stat().st_mtime < source_mtime:
            if os.environ.get("OPENBOX_REQUIRE_ARTIFACTS"):
                raise AssertionError("Bundled AppImage is older than the runtime source manifest")
            print("  skipping stale bundled AppImage; set OPENBOX_APPIMAGE to validate a rebuilt artifact")
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
        assert not missing, f"missing runtime modules in AppImage: {', '.join(missing)}"
        assert (share / "openbox-launcher.sh").is_file(), "missing keyboard launcher"
        for data in DATA_FILES:
            assert (share / data).is_file(), f"missing {data} in AppImage"
        themes = share / "themes"
        assert themes.is_dir(), "missing themes dir in AppImage"
        for name in STOCK_THEMES:
            assert (themes / name).is_file(), f"missing stock theme {name}"
        desktop = appdir / "usr" / "share" / "applications" / "io.openbox.GameLauncher.desktop"
        assert desktop.is_file(), "missing desktop file"
        if appimage_path:
            content = desktop.read_text()
            assert "Exec=AppRun %u" in content, "AppImage desktop entry must launch AppRun"
            assert "Icon=io.openbox.GameLauncher" in content, "AppImage desktop icon id must be unique"
            assert "X-AppImage-Version=" in content, "AppImage desktop entry must expose version"
        svg = appdir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "io.openbox.GameLauncher.svg"
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


def test_runtime_manifest():
    manifest = ROOT / "runtime_modules.txt"
    modules = [line.strip() for line in manifest.read_text().splitlines() if line.strip()]
    assert len(modules) == len(set(modules)), "runtime module manifest contains duplicates"
    missing = [module for module in modules if not (ROOT / module).is_file()]
    assert not missing, f"runtime module manifest has missing files: {missing}"
    build_script = (ROOT / "build_appimage.sh").read_text()
    flatpak = (ROOT / "io.openbox.GameLauncher.yml").read_text()
    assert "runtime_modules.txt" in build_script
    assert "runtime_modules.txt" in flatpak
    print("  Runtime module manifest: ok")


def test_runtime_import_closure():
    manifest = {
        line.strip() for line in (ROOT / "runtime_modules.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    local_modules = {path.stem: path.name for path in ROOT.glob("*.py")}
    missing = set()
    for filename in manifest:
        if not filename.endswith(".py"):
            continue
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"), filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".", 1)[0]]
            else:
                continue
            for name in names:
                if name in local_modules and local_modules[name] not in manifest:
                    missing.add(local_modules[name])
    assert not missing, f"runtime import closure is missing: {sorted(missing)}"
    print("  Runtime import closure: ok")


def test_flatpak_manifest():
    manifest = ROOT / "io.openbox.GameLauncher.yml"
    assert manifest.exists(), "missing Flatpak manifest"
    content = manifest.read_text()
    assert "app-id: io.openbox.GameLauncher" in content
    assert "runtime: org.freedesktop.Platform" in content
    assert "command: openbox" in content
    assert "openbox.sh" in content
    runtime_modules = (ROOT / "runtime_modules.txt").read_text()
    assert "openbox_logging.py" in runtime_modules
    assert "runtime_modules.txt" in content
    assert "openbox.svg" in content
    print("  Flatpak manifest: ok")


def test_desktop_entry():
    desktop = ROOT / "openbox.desktop"
    content = desktop.read_text()
    assert "Name=OpenBox Game Launcher" in content
    assert "Icon=io.openbox.GameLauncher" in content
    assert "Categories=Game" in content
    assert "Type=Application" in content
    assert "x-scheme-handler/openbox" in content
    print("  Desktop entry: ok")


def test_metainfo():
    xml = ROOT / "openbox.metainfo.xml"
    content = xml.read_text()
    assert "io.openbox.GameLauncher" in content
    assert '<launchable type="desktop-id">io.openbox.GameLauncher.desktop</launchable>' in content
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
    assert "| 0.6.x | Yes |" in security
    assert "| < 0.4.0 | No |" in security
    print("  Legal policy: ok")


def test_version_consistency():
    from updates import VERSION
    metainfo = ROOT / "openbox.metainfo.xml"
    content = metainfo.read_text()
    assert f'version="{VERSION}"' in content, f"Version mismatch: updates={VERSION}"
    parity = (ROOT / "PARITY.md").read_text()
    assert f"**v{VERSION}**" in parity, f"PARITY.md latest release should be v{VERSION}"
    bug_report = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
    assert f"v{VERSION}" in bug_report, f"bug_report.yml should mention v{VERSION}"
    readme = (ROOT / "README.md").read_text()
    assert f"Release-v{VERSION}" in readme, f"README release badge should be v{VERSION}"
    assert "cd OpenBoxGL" in readme, "README clone steps should cd into OpenBoxGL"
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


def test_release_appimage_workflow():
    workflow = ROOT / ".github" / "workflows" / "release-appimage.yml"
    assert workflow.is_file(), "missing release AppImage workflow"
    content = workflow.read_text()
    assert "tags:" in content and '"v*"' in content
    assert "./build_appimage.sh" in content
    assert "OpenBox-x86_64.AppImage" in content
    assert "OpenBox-x86_64.AppImage.zsync" in content
    assert "OpenBox-x86_64.AppImage.sha256" in content
    assert "sha256sum OpenBox-x86_64.AppImage" in content
    assert "softprops/action-gh-release@" in content
    assert "contents: write" in content
    print("  Release AppImage workflow: ok")


def main():
    print("packaging self-test:")
    test_desktop_entry()
    test_metainfo()
    test_legal_policy()
    test_flatpak_manifest()
    test_makefile_install()
    test_runtime_manifest()
    test_runtime_import_closure()
    test_appdir_structure()
    test_version_consistency()
    test_update_verification()
    test_appimage_update_info()
    test_release_appimage_workflow()
    print("packaging self-test: ok")


if __name__ == "__main__":
    main()
