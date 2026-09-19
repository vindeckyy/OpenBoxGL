#!/usr/bin/env python3
"""Packaging acceptance tests for OpenBox Linux distribution."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import time
import subprocess
import sys
import tempfile
import shutil
from urllib.request import urlopen
from xml.etree import ElementTree

def _posix_only(function):
    """Skip packaging checks that only apply to the POSIX/AppImage flow."""
    def wrapper():
        if os.name == "nt":
            print(f"  {function.__name__}: skipped on Windows (POSIX packaging only)")
            return
        return function()
    return wrapper


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "runtime_modules.txt").is_file():
        return candidate
    if (candidate.parent / "runtime_modules.txt").is_file():
        return candidate.parent
    return candidate

ROOT = _repo_root()
sys.path.insert(0, str(ROOT))
PYTHON_MODULES = [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
DATA_FILES = ["index.html", "openbox.svg"] + [f"static/{p.name}" for p in sorted((ROOT / "static").glob("*.js"))] + [f"static/{p.name}" for p in sorted((ROOT / "static").glob("*.css"))] + [f"locales/{p.name}" for p in sorted((ROOT / "locales").glob("*.json"))]

def _doc_path(name: str) -> Path:
    # Support both flat (docs at root) and docs/ layouts after reorg
    direct = ROOT / name
    if direct.is_file():
        return direct
    docs_path = ROOT / "docs" / name
    if docs_path.is_file():
        return docs_path
    # Fallback for adr etc. already in docs/ but name may include subpath
    return docs_path
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
            check=False,
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
            content = desktop.read_text(encoding="utf-8")
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


@_posix_only
def test_staged_install_locale_endpoints():
    """A staged Makefile install must serve every public locale endpoint."""
    locales = sorted((ROOT / "locales").glob("*.json"))
    assert locales, "expected source locale files"
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory) / "stage"
        data_dir = Path(directory) / "data"
        # Skip the native-host compile while exercising the install recipe. The
        # web server is launched with --no-browser, so /bin/true is sufficient
        # as an install-time stand-in for the native binary.
        subprocess.run(
            [
                "make", "-o", "native-host", "install",
                f"DESTDIR={stage}", "PREFIX=/usr", "NATIVE_HOST=/bin/true",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        share = stage / "usr" / "share" / "openbox"
        for locale in locales:
            installed = share / "locales" / locale.name
            assert installed.is_file(), f"missing staged locale {locale.name}"
            assert installed.read_bytes() == locale.read_bytes()

        env = os.environ.copy()
        env["OPENBOX_DATA_DIR"] = str(data_dir)
        server = subprocess.Popen(
            [sys.executable, str(share / "web_app.py"), "--no-browser"],
            cwd=share,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            port_file = data_dir / "server.port"
            deadline = time.monotonic() + 15
            while not port_file.is_file() and time.monotonic() < deadline:
                if server.poll() is not None:
                    break
                time.sleep(0.05)
            assert port_file.is_file(), "staged web server did not publish a port"
            port = int(port_file.read_text(encoding="utf-8"))
            for locale in locales:
                with urlopen(f"http://127.0.0.1:{port}/locales/{locale.name}", timeout=5) as response:
                    assert response.status == 200
                    assert response.headers["Content-Type"].startswith("application/json")
                    assert json.loads(response.read()) == json.loads(locale.read_text(encoding="utf-8"))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
            stdout, stderr = server.communicate()
            if server.returncode and not (port_file.is_file() if "port_file" in locals() else False):
                raise AssertionError(f"staged web server failed: {stderr or stdout}")
    print("  Staged locale endpoints: ok")


def test_runtime_manifest():
    manifest = ROOT / "runtime_modules.txt"
    modules = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert len(modules) == len(set(modules)), "runtime module manifest contains duplicates"
    missing = [module for module in modules if not (ROOT / module).is_file()]
    assert not missing, f"runtime module manifest has missing files: {missing}"
    assert "pkg/__init__.py" in modules, "missing pkg/__init__.py in runtime manifest"
    assert "pkg/parity/__init__.py" in modules, "missing pkg/parity/__init__.py in runtime manifest"
    for mod in modules:
        assert not mod.endswith(".pyc"), f"manifest should not contain pyc files: {mod}"
        assert not mod.startswith("build/"), f"manifest should not contain build files: {mod}"
    build_script = (ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    flatpak = (ROOT / "io.openbox.GameLauncher.yml").read_text(encoding="utf-8")
    assert "runtime_modules.txt" in build_script
    assert "runtime_modules.txt" in flatpak
    print("  Runtime module manifest: ok")


def test_runtime_import_closure():
    manifest = {
        line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # Support both flat (parity_*.py at root) and packaged (pkg/parity/) layouts
    local_stems = {}
    for path in [*ROOT.glob("*.py"), *ROOT.glob("pkg/parity/*.py")]:
        local_stems.setdefault(path.stem, path.name)
    # Also map pkg/parity names to their manifest paths for closure check
    manifest_names = {Path(m).stem: m for m in manifest if m.endswith(".py")}
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
                if name in local_stems and name not in manifest_names:
                    missing.add(local_stems[name])
    assert not missing, f"runtime import closure is missing: {sorted(missing)}"
    print("  Runtime import closure: ok")


def test_sbom_artifact_inventory():
    from scripts.gen_sbom import build_sbom

    with tempfile.TemporaryDirectory() as directory:
        appdir = Path(directory) / "OpenBox.AppDir"
        (appdir / "usr" / "bin").mkdir(parents=True)
        artifact = appdir / "usr" / "bin" / "native_host"
        artifact.write_bytes(b"native host")
        (appdir / "current").symlink_to("usr/bin/native_host")
        components = build_sbom("1.1.0", include_stdlib=False, appdir=appdir)["components"]
        by_name = {component["name"]: component for component in components}
        assert by_name["usr/bin/native_host"]["hashes"][0]["content"] == hashlib.sha256(b"native host").hexdigest()
        assert by_name["current"]["properties"][-1]["value"] == "usr/bin/native_host"
        try:
            build_sbom("1.1.0", appdir=appdir / "missing")
            raise AssertionError("missing AppDir should fail")
        except ValueError:
            pass
    print("  SBOM AppDir inventory: ok")


def test_sbom_deterministic_output():
    with tempfile.TemporaryDirectory() as directory:
        out_path = Path(directory) / "sbom.json"
        cmd = [sys.executable, str(ROOT / "scripts" / "gen_sbom.py"), str(out_path)]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert out_path.is_file(), f"gen_sbom did not create {out_path}"
        content = out_path.read_text(encoding="utf-8")
        data = json.loads(content)
        # Verify deterministic sorted keys at top level
        assert list(data.keys()) == sorted(data.keys()), "top-level keys not sorted"
        assert "components" in data
        # Verify every component has sorted keys
        for comp in data["components"]:
            assert list(comp.keys()) == sorted(comp.keys()), f"component keys not sorted in {comp.get('name')}"
        # Also verify build/sbom.json path execution
        build_sbom_path = ROOT / "build" / "sbom.json"
        cmd_build = [sys.executable, str(ROOT / "scripts" / "gen_sbom.py"), str(build_sbom_path)]
        subprocess.run(cmd_build, capture_output=True, text=True, check=True)
        assert build_sbom_path.is_file()
        build_data = json.loads(build_sbom_path.read_text(encoding="utf-8"))
        assert list(build_data.keys()) == sorted(build_data.keys())
    print("  SBOM deterministic output: ok")

def test_sbom_hash_verification():
    from scripts.gen_sbom import build_sbom
    from updates import VERSION
    sbom = build_sbom(VERSION, include_stdlib=False)
    for comp in sbom["components"]:
        if comp.get("type") in ("library", "file") and "hashes" in comp:
            file_path = ROOT / comp["name"]
            if file_path.is_file():
                expected_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                actual_hash = comp["hashes"][0]["content"]
                assert actual_hash == expected_hash, f"Hash mismatch for {comp['name']}: {actual_hash} != {expected_hash}"
    print("  SBOM hash verification: ok")


def _windows_powershell():
    """The Windows PowerShell host, when this machine has one."""
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_launcher(launcher, arguments, share_dir, local_app_data=None):
    """Run a Windows launcher against a stand-in web app and return the result."""
    env = dict(os.environ)
    env["OPENBOX_SHARE"] = str(share_dir)
    env["OPENBOX_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(ROOT)
    if local_app_data is not None:
        # Keeps the install-dir candidate from reaching a real %LOCALAPPDATA%
        # install while the launcher resolves its share dir.
        env["LOCALAPPDATA"] = str(local_app_data)
    return subprocess.run(
        [_windows_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(launcher), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120, check=False,
    )


def test_windows_launchers():
    """Each Windows entry point resolves the share dir and forwards its flags."""
    if os.name != "nt" or not _windows_powershell():
        print("  Windows launchers: skipped (needs Windows PowerShell)")
        return
    for name in ("openbox.cmd", "openbox.ps1", "openbox-native.ps1"):
        assert (ROOT / name).is_file(), f"missing Windows launcher: {name}"

    with tempfile.TemporaryDirectory() as directory:
        share = Path(directory) / "share"
        share.mkdir()
        marker = Path(directory) / "argv.txt"
        # The stand-in records the flags it was handed, so a launcher that
        # resolved the wrong share dir or dropped arguments cannot pass.
        (share / "web_app.py").write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8",
        )

        # ``--web`` opts out of the native host and forwards the rest verbatim.
        result = _run_launcher(ROOT / "openbox.ps1", ["--web", "--no-browser", "--width", "1024"], share)
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.read_text(encoding="utf-8") == "--no-browser --width 1024"

        # Without a native host the native launcher falls back to the web app
        # instead of failing.
        marker.unlink()
        result = _run_launcher(ROOT / "openbox-native.ps1", ["--no-browser", "--width", "1024"], share)
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.read_text(encoding="utf-8") == "--no-browser --width 1024"
        # Write-Warning lands on stdout when PowerShell's streams are redirected.
        assert "falling back" in result.stdout + result.stderr

        # A launcher that cannot find web_app.py must fail loudly rather than
        # start something arbitrary. Its own directory is a candidate, so this
        # needs a copy outside the repo with every candidate pointed elsewhere.
        elsewhere = Path(directory) / "elsewhere"
        elsewhere.mkdir()
        shutil.copy2(ROOT / "openbox.ps1", elsewhere / "openbox.ps1")
        result = _run_launcher(elsewhere / "openbox.ps1", ["--web"], elsewhere, local_app_data=elsewhere)
        assert result.returncode != 0
        assert "web_app.py" in result.stdout + result.stderr
    print("  Windows launchers: ok")


def test_windows_installer_contract():
    """scripts/install.ps1 pins the committed release key and parses."""
    installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    # The bootstrap anchor is the only thing standing between a download and an
    # install, so it must track the committed public key exactly.
    anchor = hashlib.sha256((ROOT / "openbox-release.pub").read_bytes()).hexdigest()
    assert anchor in installer, "install.ps1 must pin the committed release public key"
    assert "share\\openbox" in installer, "install.ps1 must install into the share dir the launchers resolve"
    assert "updates.py" in installer, "install.ps1 must verify signatures with the in-repo verifier"

    if os.name != "nt" or not _windows_powershell():
        print("  Windows installer: contract ok (parse check needs Windows PowerShell)")
        return
    env = dict(os.environ)
    for name in ("scripts/install.ps1", "openbox.ps1", "openbox-native.ps1"):
        env["OPENBOX_SCRIPT_UNDER_TEST"] = str(ROOT / name)
        result = subprocess.run(
            [_windows_powershell(), "-NoProfile", "-Command",
             "$null = [scriptblock]::Create((Get-Content -Raw -LiteralPath $env:OPENBOX_SCRIPT_UNDER_TEST))"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120, check=False,
        )
        assert result.returncode == 0, f"{name} does not parse: {result.stderr}"
    print("  Windows installer: ok")


def test_build_appimage_validation():
    build_script = (ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    assert "runtime_modules.txt" in build_script
    assert "missing runtime module" in build_script
    assert "sbom-manifest.json" in build_script
    assert "gen_sbom.py" in build_script

    if os.name == "nt":
        # The validation loop below is bash; the AppImage build has no Windows
        # counterpart, so only the script references are checked here.
        print("  Build AppImage validation: script references ok (bash loop skipped on Windows)")
        return

    # Test validation failure on missing module in a mock loop
    test_script = """
set -e
source_root="%s"
while IFS= read -r file; do
  file="$(echo "$file" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -n "$file" ] || continue
  [[ "$file" =~ ^# ]] && continue
  if [ ! -f "$source_root/$file" ]; then
    echo "missing runtime module: $file" >&2
    exit 1
  fi
done <<'EOF'
openbox.py
nonexistent_test_module_xyz.py
EOF
""" % ROOT
    res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True, check=False)
    assert res.returncode != 0
    assert "missing runtime module: nonexistent_test_module_xyz.py" in res.stderr
    print("  Build AppImage validation: ok")

def _ci_job_block(job_name: str) -> str:
    content = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"{job_name}:" in content, f"missing CI job {job_name}"
    block = content.split(f"{job_name}:", 1)[1]
    next_job = re.search(r"\n  [a-z0-9-]+:\n", block)
    if next_job:
        block = block[: next_job.start()]
    assert "continue-on-error: true" not in block, f"{job_name} must not use continue-on-error"
    return block


def test_desktop_and_appstream_validate_subprocess():
    if shutil.which("desktop-file-validate") is None or shutil.which("appstreamcli") is None:
        print("  Desktop/AppStream validators: skipped (tools not installed)")
        return
    subprocess.run(["desktop-file-validate", str(ROOT / "openbox.desktop")], check=True)
    subprocess.run(
        ["appstreamcli", "validate", "--no-net", str(ROOT / "openbox.metainfo.xml")],
        check=True,
    )
    print("  Desktop/AppStream validators: ok")


def test_ci_desktop_appstream_job():
    block = _ci_job_block("desktop-appstream")
    assert "desktop-file-validate openbox.desktop" in block
    assert "appstreamcli validate --no-net openbox.metainfo.xml" in block
    print("  CI desktop-appstream job: ok")


def _flatpak_dry_run_supported() -> bool:
    if shutil.which("flatpak-builder") is None:
        return False
    probe = subprocess.run(
        ["flatpak-builder", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode != 1 or "Unknown option" not in (probe.stderr or "")


def test_flatpak_validate_subprocess():
    if _flatpak_dry_run_supported():
        subprocess.run(
            [
                "flatpak-builder",
                "--dry-run",
                "--force-clean",
                "/tmp/ob-flatpak-dry",
                str(ROOT / "io.openbox.GameLauncher.yml"),
            ],
            check=True,
        )
    else:
        print("  Flatpak dry-run: skipped locally (unsupported; CI flatpak-validate is required gate)")
        subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "validate_flatpak_manifest.py")],
            check=True,
        )
    if shutil.which("appstreamcli") is None:
        print("  Flatpak dry-run validator: skipped (appstreamcli not installed)")
        return
    subprocess.run(
        ["appstreamcli", "validate", "--no-net", str(ROOT / "openbox.metainfo.xml")],
        check=True,
    )
    print("  Flatpak dry-run validator: ok")


def test_ci_flatpak_validate_job():
    block = _ci_job_block("flatpak-validate")
    assert "flatpak-builder --dry-run --force-clean /tmp/ob-flatpak-dry io.openbox.GameLauncher.yml" in block
    print("  CI flatpak-validate job: ok")


def test_flatpak_manifest():
    manifest = ROOT / "io.openbox.GameLauncher.yml"
    assert manifest.exists(), "missing Flatpak manifest"
    content = manifest.read_text(encoding="utf-8")
    assert "app-id: io.openbox.GameLauncher" in content
    assert "runtime: org.gnome.Platform" in content
    assert "runtime-version: '49'" in content
    assert "command: openbox" in content
    assert "openbox.sh" in content
    runtime_modules = (ROOT / "runtime_modules.txt").read_text(encoding="utf-8")
    assert "openbox_logging.py" in runtime_modules
    assert "openbox-release.pub" in runtime_modules
    assert "runtime_modules.txt" in content
    assert "openbox.svg" in content
    print("  Flatpak manifest: ok")


def test_flatpak_icon_is_square():
    root = ElementTree.parse(ROOT / "openbox.svg").getroot()
    view_box = [float(value) for value in (root.get("viewBox") or "").split()]
    assert len(view_box) == 4, "openbox.svg must declare a four-value viewBox"
    assert view_box[2] == view_box[3], "Flatpak app icons must use a square viewBox"
    assert float(root.get("width", "0")) == float(root.get("height", "-1"))
    print("  Flatpak icon geometry: square")


def test_release_flatpak_workflow():
    workflow = ROOT / ".github" / "workflows" / "release-flatpak.yml"
    assert workflow.is_file(), "missing release Flatpak workflow"
    content = workflow.read_text(encoding="utf-8")
    assert "ubuntu-24.04" in content
    assert "io.openbox.GameLauncher.yml" in content
    assert "flatpak build-bundle" in content
    assert "group: release-publish-${{ github.ref_name }}" in content
    assert "body_path: docs/RELEASE_NOTES.md" in content
    appimage = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "group: release-publish-${{ github.ref_name }}" in appimage
    print("  Release Flatpak workflow: ok")


def test_desktop_entry():
    desktop = ROOT / "openbox.desktop"
    content = desktop.read_text(encoding="utf-8")
    assert "Name=OpenBox Game Launcher" in content
    assert "Icon=io.openbox.GameLauncher" in content
    assert "Categories=Game" in content
    assert "Type=Application" in content
    assert "x-scheme-handler/openbox" in content
    print("  Desktop entry: ok")


def test_metainfo():
    xml = ROOT / "openbox.metainfo.xml"
    content = xml.read_text(encoding="utf-8")
    assert "io.openbox.GameLauncher" in content
    assert '<launchable type="desktop-id">io.openbox.GameLauncher.desktop</launchable>' in content
    assert "<name>OpenBox Game Launcher</name>" in content
    assert "unrelated to the Openbox Linux window manager" in content
    assert "AGPL-3.0" in content
    assert "release version" in content
    print("  Metainfo XML: ok")


def test_legal_policy():
    from updates import VERSION

    disclaimer = (_doc_path("DISCLAIMER.md")).read_text(encoding="utf-8")
    trademarks = (_doc_path("TRADEMARKS.md")).read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    security = (_doc_path("SECURITY.md")).read_text(encoding="utf-8")
    assert "https://github.com/contact/dmca" in disclaimer
    assert "github-trademark-policy" in disclaimer
    assert "security/advisories/new" in disclaimer
    assert "TRADEMARKS.md" in disclaimer and "TRADEMARKS.md" in readme
    assert "clean-room" not in disclaimer
    assert "15 U.S.C." not in disclaimer
    assert "Openbox window manager" in trademarks
    assert "| 0.8.x | No — upgrade required |" in security
    assert "| 1.0.x | No — upgrade required |" in security
    # The supported-versions table must mark the minor line of the declared
    # version as current, so a release only has to bump updates.py.
    supported = ".".join(VERSION.split(".")[:2])
    assert f"| {supported}.x | Yes (current) |" in security, f"SECURITY.md should mark {supported}.x current"
    assert "| 1.11.x | No — upgrade required |" in security
    assert "| < 0.4.0 | No |" in security
    print("  Legal policy: ok")


def test_version_consistency():
    from updates import VERSION
    metainfo = ROOT / "openbox.metainfo.xml"
    content = metainfo.read_text(encoding="utf-8")
    assert f'version="{VERSION}"' in content, f"Version mismatch: updates={VERSION}"
    parity = (_doc_path("PARITY.md")).read_text(encoding="utf-8")
    assert f"**v{VERSION}**" in parity, f"PARITY.md latest release should be v{VERSION}"
    bug_report = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    assert f"v{VERSION}" in bug_report, f"bug_report.yml should mention v{VERSION}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Release-v{VERSION}" in readme, f"README release badge should be v{VERSION}"
    assert "cd OpenBoxGL" in readme, "README clone steps should cd into OpenBoxGL"
    print("  Version consistency: ok")


def test_update_verification():
    from updates import version_tuple
    assert version_tuple("0.1.0") < version_tuple("0.2.0")
    assert version_tuple("1.0.0") > version_tuple("0.9.9")
    assert version_tuple("1.2.3") == version_tuple("1.2.3")
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "RELEASE_KEY_SHA256" in installer
    assert "openssl pkeyutl -verify" in installer
    assert "SIG_ASSET" in installer
    assert 'OPENBOX_ARCH' in installer
    assert 'OpenBox-${arch}.AppImage' in installer
    assert 'aarch64|arm64' in installer
    pinned = re.search(r'RELEASE_KEY_SHA256="([0-9a-f]{64})"', installer)
    assert pinned, "installer must pin a SHA-256 trust anchor"
    assert pinned.group(1) == hashlib.sha256((ROOT / "openbox-release.pub").read_bytes()).hexdigest()
    print("  Update version logic: ok")


def test_appimage_update_info():
    build_script = ROOT / "build_appimage.sh"
    content = build_script.read_text(encoding="utf-8")
    assert "OPENBOX_UPDATE_INFORMATION" in content
    assert "gh-releases-zsync|vindeckyy|OpenBoxGL|latest" in content
    assert "OPENBOX_APPDIR" in content
    assert "tool_sha256" in content
    print("  AppImage update info: ok")


@_posix_only
def test_appimage_library_scope():
    build_script = (ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    app_run = build_script.split('install -m 755 /dev/stdin "$appdir/AppRun" <<\'EOF\'', 1)[1].split("\nEOF\n", 1)[0]
    native_launcher = (ROOT / "openbox-native.sh").read_text(encoding="utf-8")
    assert "OPENBOX_BUNDLED_LIB_PATH" in app_run
    assert "unset LD_LIBRARY_PATH" in app_run
    assert "export LD_LIBRARY_PATH" not in app_run
    assert "env LD_LIBRARY_PATH=\"$OPENBOX_BUNDLED_LIB_PATH\" \"$HOST_BIN\"" in native_launcher
    assert "env LD_LIBRARY_PATH=\"$OPENBOX_BUNDLED_LIB_PATH\" \"${OPENBOX_PYTHON:-python3}\"" in native_launcher
    assert "export LD_LIBRARY_PATH" not in native_launcher
    # The shell launcher itself runs without the bundled library path. Only
    # the native binary receives it, so host shell commands cannot resolve
    # AppImage-provided readline or ncurses symbols.
    assert app_run.index("unset LD_LIBRARY_PATH") < app_run.index("openbox-native.sh")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        share = root / "usr" / "share" / "openbox"
        (root / "usr" / "bin").mkdir(parents=True)
        share.mkdir(parents=True)
        app_run_path = root / "AppRun"
        app_run_path.write_text("#!/bin/bash\n" + app_run + "\n", encoding="utf-8")
        app_run_path.chmod(0o755)
        (share / "openbox-native.sh").write_text((ROOT / "openbox-native.sh").read_text(encoding="utf-8"))
        (share / "openbox-native.sh").chmod(0o755)
        marker = root / "native-env"
        native = share / "native_host"
        native.write_text("#!/bin/sh\nprintf '%s' \"$LD_LIBRARY_PATH\" > \"$OPENBOX_TEST_ENV\"\n", encoding="utf-8")
        native.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "HOME": str(root),
            "OPENBOX_DATA_DIR": str(root / "data"),
            "OPENBOX_TEST_ENV": str(marker),
            "LD_LIBRARY_PATH": "/host/incompatible/readline",
        })
        subprocess.run([str(app_run_path)], env=env, check=True, timeout=10)
        assert marker.read_text(encoding="utf-8") == f"{root / 'usr' / 'lib'}:{root / 'usr' / 'lib64'}"

        marker.unlink()
        native.unlink()
        python = root / "usr" / "bin" / "python3"
        python.write_text("#!/bin/sh\nprintf '%s' \"$LD_LIBRARY_PATH\" > \"$OPENBOX_TEST_ENV\"\n", encoding="utf-8")
        python.chmod(0o755)
        subprocess.run([str(app_run_path)], env=env, check=True, timeout=10)
        assert marker.read_text(encoding="utf-8") == f"{root / 'usr' / 'lib'}:{root / 'usr' / 'lib64'}"
    print("  AppImage library scope: ok")


def test_release_workflow():
    workflow = ROOT / ".github" / "workflows" / "release.yml"
    assert workflow.is_file(), "missing release workflow"
    content = workflow.read_text(encoding="utf-8")
    assert "ubuntu-22.04" in content
    assert "ubuntu-latest" not in content
    assert "tags:" in content and '"v*"' in content
    assert "./build_appimage.sh" in content
    assert "target_commitish: ${{ github.sha }}" in content
    # Two-arch build matrix (ADR 0024): x86_64 on ubuntu-22.04, aarch64 on
    # ubuntu-24.04-arm (ubuntu-22.04-arm deprecates 2026-09-17).
    assert "matrix.arch" in content
    assert "ubuntu-24.04-arm" in content
    assert "OPENBOX_ARCH:" in content
    assert 'sha256sum "OpenBox-${{ matrix.arch }}.AppImage"' in content
    # Both arches are published as signed release assets.
    assert "OpenBox-x86_64.AppImage" in content
    assert "OpenBox-x86_64.AppImage.zsync" in content
    assert "OpenBox-x86_64.AppImage.sha256" in content
    assert "OpenBox-x86_64.AppImage.sig" in content
    assert "OpenBox-aarch64.AppImage" in content
    assert "OpenBox-aarch64.AppImage.zsync" in content
    assert "OpenBox-aarch64.AppImage.sha256" in content
    assert "OpenBox-aarch64.AppImage.sig" in content
    assert "openbox-release.pub" in content
    assert "OPENBOX_SIGNING_KEY is required" in content
    assert "persist-credentials: false" in content
    assert "actions/upload-artifact@" in content
    assert "overwrite_files: false" in content
    assert "softprops/action-gh-release@" in content
    assert "contents: write" in content
    # The Windows portable channel is built, attested and signed by the same
    # release (ADR 0048).
    assert "windows-latest" in content
    assert "openbox-release-assets-windows" in content
    assert "OpenBox-x86_64-windows.zip" in content
    assert "OpenBox-x86_64-windows.zip.sig" in content
    assert "scripts/install.ps1" in content
    print("  Release workflow: ok")
def test_markdown_locations():
    root_md = {p.name for p in ROOT.glob("*.md")}
    approved_root_md = {"README.md", "AGENTS.md", "CLAUDE.md", "ARCHITECTURE.md"}
    assert root_md.issubset(approved_root_md), f"unexpected root markdown files: {root_md - approved_root_md}"
    assert "RELEASE_NOTES.md" not in root_md, "RELEASE_NOTES.md must live in docs/ not at root"
    assert (ROOT / "docs" / "RELEASE_NOTES.md").is_file(), "missing docs/RELEASE_NOTES.md"
    print("  Markdown locations: ok")


def main():
    print("packaging self-test:")
    test_desktop_entry()
    test_metainfo()
    test_desktop_and_appstream_validate_subprocess()
    test_ci_desktop_appstream_job()
    test_flatpak_validate_subprocess()
    test_ci_flatpak_validate_job()
    test_release_flatpak_workflow()
    test_legal_policy()
    test_flatpak_manifest()
    test_flatpak_icon_is_square()
    test_makefile_install()
    test_staged_install_locale_endpoints()
    test_runtime_manifest()
    test_runtime_import_closure()
    test_sbom_artifact_inventory()
    test_sbom_deterministic_output()
    test_appdir_structure()
    test_version_consistency()
    test_update_verification()
    test_appimage_update_info()
    test_appimage_library_scope()
    test_release_workflow()
    test_windows_launchers()
    test_windows_installer_contract()
    test_markdown_locations()
    test_sbom_hash_verification()
    test_build_appimage_validation()
    print("packaging self-test: ok")


if __name__ == "__main__":
    main()
