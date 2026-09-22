"""Release-pipeline dry-run: tag and asset gates, without publishing anything.

Honest scope: this tests the *gate logic* of .github/workflows/release.yml —
the tag-format check really validates tags, validation runs before any
destructive step, and the required-asset ladder refuses a missing artifact
before anything is signed or uploaded — plus a Python mirror of the zip-entry
gate the Windows packaging step enforces. It does not execute the workflow
YAML itself, and the zip check mirrors the PowerShell gate logic rather than
running it.
"""

import re
import tempfile
import zipfile
from pathlib import Path


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "runtime_modules.txt").is_file():
        return candidate
    if (candidate.parent / "runtime_modules.txt").is_file():
        return candidate.parent
    return candidate


ROOT = _repo_root()
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> str:
    assert WORKFLOW.is_file(), "missing release workflow"
    return WORKFLOW.read_text(encoding="utf-8")


def _job_block(content: str, job: str) -> str:
    """The raw YAML text of one job (up to the next job or end of file)."""
    tail = content.split(f"\n  {job}:", 1)[1]
    nxt = re.search(r"\n  [a-z0-9-]+:\n", tail)
    return tail[: nxt.start()] if nxt else tail


def _job_steps(content: str, job: str) -> list:
    """Step names of one workflow job, in order."""
    return re.findall(r"^      - name:\s*(.+?)\s*$", _job_block(content, job), re.M)


def _assert_ordered(steps: list, gate: str, destructive: list) -> None:
    assert gate in steps, f"missing gate step: {gate}"
    for step in destructive:
        assert step in steps, f"missing destructive step: {step}"
        assert steps.index(gate) < steps.index(step), \
            f"{gate!r} must run before {step!r}"


def _release_tag_patterns() -> list:
    """The tag regexes the build jobs enforce, extracted from the workflow."""
    content = _workflow()
    build = _job_block(content, "build")
    windows = _job_block(content, "build-windows")
    # Python's `re` reproduces the ERE semantics of these constructs, so the
    # extracted patterns can be exercised directly as a dry-run.
    bash = re.search(r'\[\[ "\$GITHUB_REF_NAME" =~ (.+?) \]\]', build)
    pwsh = re.search(r"-notmatch '(.+?)'", windows)
    assert bash and pwsh, "both build jobs must validate the release tag format"
    return [bash.group(1), pwsh.group(1)]


def test_tag_format_gate():
    """The pipeline must validate the tag format before doing anything destructive."""
    patterns = _release_tag_patterns()
    assert len(patterns) == 2
    assert patterns[0] == patterns[1], "both build jobs must enforce the same tag pattern"
    pattern = patterns[0]
    for good in ("v1.13.1", "v0.4.0", "v1.13.1-rc1", "v10.20.30"):
        assert re.match(pattern, good), f"valid release tag rejected: {good}"
    # Note: the pattern's suffix class ([-.][0-9A-Za-z.-]+) deliberately
    # accepts dot-led numeric suffixes too, so "v1.13.1.4" parses as a
    # suffixed tag rather than a four-part version.
    for bad in ("1.13.1", "v1.13", "vX", "", "latest", "v1.13.1;id", "v 1.13.1"):
        assert not re.match(pattern, bad), f"invalid release tag accepted: {bad!r}"
    # The gate also pins the tag to the declared version, so a mistagged
    # release can never publish: both jobs compare against updates.py.
    content = _workflow()
    assert "does not match updates.py version" in _job_block(content, "build")
    assert "does not match updates.py version" in _job_block(content, "build-windows")
    print("  release tag format gate: ok")


def test_validation_runs_before_destructive_steps():
    """Tag validation must precede every destructive step in each job."""
    content = _workflow()
    _assert_ordered(
        _job_steps(content, "build"),
        "Validate release tag",
        ["Build AppImage", "Upload unsigned build outputs"],
    )
    _assert_ordered(
        _job_steps(content, "build-windows"),
        "Validate release tag",
        ["Build the WebView2 native host", "Package the portable release",
         "Upload unsigned build outputs"],
    )
    _assert_ordered(
        _job_steps(content, "publish"),
        "Validate annotated tag points at the build commit",
        ["Sign and verify release artifacts", "Create release and upload signed assets"],
    )
    print("  validation before destructive steps: ok")


def test_required_assets_checked_before_publish():
    """Missing build artifacts must fail the release before anything is signed."""
    content = _workflow()
    publish = _job_block(content, "publish")
    sign_block = publish.split("Sign and verify release artifacts", 1)[1].split(
        "Create release and upload signed assets", 1)[0]
    # Every signed artifact is refused when its file is absent, and the
    # signing key must match the committed release key before use.
    assert sign_block.count("missing build artifact") >= 3
    assert "OPENBOX_SIGNING_KEY is required" in sign_block
    assert "does not match the committed release public key" in sign_block
    assert "sha256sum --check --strict" in sign_block
    upload_block = publish.split("Create release and upload signed assets", 1)[1]
    for asset in (
        "OpenBox-x86_64.AppImage", "OpenBox-aarch64.AppImage",
        "OpenBox-x86_64-windows.zip", "OpenBox-x86_64-windows-native-host.exe",
    ):
        assert asset in upload_block, f"publish step must upload {asset}"
        assert f"{asset}.sig" in upload_block, f"publish step must upload {asset}.sig"
    print("  required assets checked before publish: ok")


def _zip_entry_gate(path: Path) -> str:
    """Python mirror of the release.yml Windows packaging gate.

    Exactly one top-level folder, no top-level files, and the native host
    binary inside it. Entry separators are normalized the way the PowerShell
    check normalizes them (Compress-Archive uses "\\" on PowerShell 5.1).
    """
    with zipfile.ZipFile(path) as bundle:
        names = [name.replace("\\", "/").strip("/") for name in bundle.namelist()]
    names = [name for name in names if name]
    top_files = {name for name in names if "/" not in name}
    top_folders = {name.split("/")[0] for name in names if "/" in name}
    if len(top_folders) != 1 or top_files:
        raise ValueError("The release archive must contain exactly one top-level folder.")
    root = next(iter(top_folders))
    if f"{root}/native_host.exe" not in names:
        raise ValueError("the release archive is missing OpenBox/native_host.exe")
    if f"{root}/web_app.py" not in names:
        raise ValueError("The release archive does not contain web_app.py.")
    return root


def test_windows_zip_entry_gate():
    """The zip-entry gate logic: one folder, the host binary, the app entry."""
    with tempfile.TemporaryDirectory() as directory:
        good = Path(directory) / "good.zip"
        with zipfile.ZipFile(good, "w") as bundle:
            bundle.writestr("OpenBox/web_app.py", "# app\n")
            # PowerShell 5.1 Compress-Archive emits backslash separators.
            bundle.writestr("OpenBox\\native_host.exe", "MZ")
        assert _zip_entry_gate(good) == "OpenBox"

        two_roots = Path(directory) / "two.zip"
        with zipfile.ZipFile(two_roots, "w") as bundle:
            bundle.writestr("OpenBox/web_app.py", "# app\n")
            bundle.writestr("OpenBox/native_host.exe", "MZ")
            bundle.writestr("Extra/readme.txt", "x")
        try:
            _zip_entry_gate(two_roots)
            raise AssertionError("two top-level folders must be rejected")
        except ValueError as error:
            assert "exactly one top-level folder" in str(error)

        top_file = Path(directory) / "topfile.zip"
        with zipfile.ZipFile(top_file, "w") as bundle:
            bundle.writestr("OpenBox/web_app.py", "# app\n")
            bundle.writestr("OpenBox/native_host.exe", "MZ")
            bundle.writestr("loose.txt", "x")
        try:
            _zip_entry_gate(top_file)
            raise AssertionError("top-level files must be rejected")
        except ValueError as error:
            assert "exactly one top-level folder" in str(error)

        no_host = Path(directory) / "nohost.zip"
        with zipfile.ZipFile(no_host, "w") as bundle:
            bundle.writestr("OpenBox/web_app.py", "# app\n")
        try:
            _zip_entry_gate(no_host)
            raise AssertionError("a missing native_host.exe must be rejected")
        except ValueError as error:
            assert "native_host.exe" in str(error)
    print("  windows zip entry gate: ok")


def main():
    print("release workflow dry-run:")
    test_tag_format_gate()
    test_validation_runs_before_destructive_steps()
    test_required_assets_checked_before_publish()
    test_windows_zip_entry_gate()
    print("release workflow dry-run: ok")


if __name__ == "__main__":
    main()
