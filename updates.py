"""Verified GitHub release updates for OpenBox.

Each platform has one release channel: an AppImage on Linux, a portable zip
installed under ``%LOCALAPPDATA%\\OpenBox`` on Windows. Both verify the same
Artifact: a SHA-256 checksum plus an Ed25519 signature from the committed
release key; only the install step differs.
"""

import base64
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend_io import atomic_write_bytes, atomic_write_text, download_file, fsync_directory, read_limited
from pkg.platform_compat import IS_WINDOWS, start_menu_programs_dir, windows_install_dir

logger = logging.getLogger("openbox")

VERSION = "1.13.1"
RELEASE_API = "https://api.github.com/repos/vindeckyy/OpenBoxGL/releases/latest"
TRUSTED_RELEASE_PREFIX = "https://github.com/vindeckyy/OpenBoxGL/releases/download/"


def _current_arch(machine=None):
    """Map the host CPU to the OpenBox artifact architecture tag."""
    machine = machine or platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return machine


def _arch_asset(arch):
    """The release asset name for a given architecture on this platform."""
    if IS_WINDOWS:
        return f"OpenBox-{arch}-windows.zip"
    return f"OpenBox-{arch}.AppImage"


# The updater only ever installs an artifact matching the running architecture;
# a release without that asset is treated as unavailable (see check_update).
ASSET = _arch_asset(_current_arch())

# Legacy bootstrap key. Releases signed with this value are rejected.
PLACEHOLDER_PUBLIC_KEY = bytes.fromhex(
    "9df1f9e7cdba094ac9d858d541b7529c28329a309ff79a4812457eb3f259fa8d"
)
PUBLIC_KEY_PATH = Path(__file__).resolve().parent / "openbox-release.pub"
SIGNATURE_ASSET = f"{ASSET}.sig"


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if not match:
        raise ValueError("The release has an invalid version.")
    return tuple(map(int, match.groups()))


def _version_key(value):
    """Compare with pre-release/build suffix awareness (suffix sorts lower)."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)([-+].*)?", str(value).strip())
    if not match:
        raise ValueError("The release has an invalid version.")
    return tuple(map(int, match.groups()[:3])) + (1 if not match.group(4) else 0,)

def github_request(url, opener=urlopen):
    from env_config import github_token_from_env

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"OpenBox/{VERSION}",
    }
    token = github_token_from_env()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return opener(Request(url, headers=headers), timeout=30)


def asset_digest(asset):
    digest = str(asset.get("digest", "")).strip()
    if digest.startswith("sha256:"):
        value = digest.split(":", 1)[1].lower()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def parse_release_assets(release):
    urls = {}
    digests = {}
    for asset in release.get("assets", []):
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).strip()
        if not name:
            continue
        url = str(asset.get("browser_download_url", "")).strip()
        if url:
            urls[name] = url
        digest = asset_digest(asset)
        if digest:
            digests[name] = digest
    return urls, digests


def load_checksum_file(url, opener=urlopen):
    with github_request(url, opener=opener) as response:
        parts = read_limited(response, 4096).decode().split()
    if not parts:
        raise ValueError("The release checksum is invalid.")
    expected = parts[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The release checksum is invalid.")
    return expected


def resolve_update_checksum(update, opener=urlopen):
    checksum = str(update.get("checksum", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", checksum):
        return checksum
    checksum_url = str(update.get("checksum_url", "")).strip()
    if checksum_url.startswith(TRUSTED_RELEASE_PREFIX):
        return load_checksum_file(checksum_url, opener=opener)
    raise ValueError("The release checksum is unavailable.")


# Every y-coordinate that encodes a small-order Ed25519 point (orders 1, 2,
# 4 and 8), matching the libsodium/ZIP-215 blacklist. Both sign-bit variants
# share a y value, and a small-order verification key makes signatures
# forgeable, so all of them must be rejected.
_SMALL_ORDER_Y = frozenset(
    int.from_bytes(bytes.fromhex(encoding), "little") & ((1 << 255) - 1)
    for encoding in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    )
)


def _point_decompress(public_bytes):
    """Decompress an Ed25519 public key to affine coordinates (RFC 8032).

    Mirrors scripts/verify_release.py; kept stdlib-only because scripts/ is
    not shipped inside the AppImage runtime.
    """
    p = 2 ** 255 - 19
    d = (-121665 * pow(121666, p - 2, p)) % p
    y = int.from_bytes(public_bytes, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    # Reject encodings with y >= p (non-canonical field elements). The
    # sign bit must be masked before the range check (RFC 8032).
    if y >= p:
        raise ValueError("Invalid Ed25519 point: coordinate out of range.")
    # Reject small-order points (orders 1, 2, 4 and 8): they are never
    # valid verification keys for this application.
    if y in _SMALL_ORDER_Y:
        raise ValueError("Invalid Ed25519 point: small-order point.")
    denominator = (d * y * y + 1) % p
    x2 = ((y * y - 1) * pow(denominator, p - 2, p)) % p
    x = pow(x2, (p + 3) // 8, p)
    if (x * x) % p != x2:
        x = (x * pow(2, (p - 1) // 4, p)) % p
        if (x * x) % p != x2:
            raise ValueError("Invalid Ed25519 point: not on the curve.")
    if (x & 1) != sign:
        x = p - x
    return x, y


def _verify_ed25519(public_bytes, signature, message):
    """RFC 8032 Ed25519 verification with stdlib big ints."""
    p = 2 ** 255 - 19
    L = 2 ** 252 + 27742317777372353535851937790883648493
    Bx = 15112221349535400772501151409588531511454012693041857206046113283949847762202
    By = 46316835694926478169428394003475163141307993866256225615783033603165251855960
    d = (-121665 * pow(121666, p - 2, p)) % p

    if len(public_bytes) != 32 or len(signature) != 64:
        raise ValueError("Invalid Ed25519 key or signature length.")

    # Canonical scalar check must happen before any point arithmetic so
    # malformed signatures fail cleanly.
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False

    A = _point_decompress(public_bytes)
    R = _point_decompress(signature[:32])
    h = hashlib.sha512(signature[:32] + public_bytes + message).digest()
    k = int.from_bytes(h, "little") % L

    def point_add(P, Q):
        x1, y1, x2, y2 = P[0], P[1], Q[0], Q[1]
        x3 = ((x1 * y2 + y1 * x2) * pow(1 + d * x1 * x2 * y1 * y2, p - 2, p)) % p
        y3 = ((y1 * y2 + x1 * x2) * pow(1 - d * x1 * x2 * y1 * y2, p - 2, p)) % p
        return (x3, y3)

    def point_mul(n, P):
        result = None
        addend = P
        while n:
            if n & 1:
                result = addend if result is None else point_add(result, addend)
            addend = point_add(addend, addend)
            n >>= 1
        return result

    kA = point_mul(k, A)
    if kA is None:
        kA = (0, 1)
    return point_mul(s, (Bx, By)) == point_add(R, kA)


def load_release_signature(url, opener=urlopen):
    """Fetch and parse a release .sig payload (same JSON contract as sign_release.py)."""
    with github_request(url, opener=opener) as response:
        payload = json.loads(read_limited(response, 64 * 1024).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("The release signature is invalid.")
    if payload.get("algorithm") != "ed25519":
        raise ValueError(f"The release signature uses an unsupported algorithm: {payload.get('algorithm')!r}")
    if payload.get("digest_algorithm") != "sha256":
        raise ValueError("The release signature is missing a SHA-256 digest.")
    digest = str(payload.get("digest", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("The release signature is missing a valid digest.")
    try:
        signature = base64.b64decode(str(payload.get("signature", "")), validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("The release signature is missing a valid signature.") from error
    if len(signature) != 64:
        raise ValueError("The release signature has an invalid length.")
    return {"digest": digest, "signature": signature}


def verify_update_signature(update, artifact_digest, signature, public_key_bytes):
    """Verify a release signature against the artifact digest and committed public key."""
    if len(public_key_bytes) != 32:
        raise ValueError("The release public key is invalid.")
    if signature["digest"] != artifact_digest:
        raise ValueError("The release signature does not match the artifact digest.")
    if not _verify_ed25519(public_key_bytes, signature["signature"], bytes.fromhex(artifact_digest)):
        raise ValueError("The release signature verification failed.")
    return True


def _release_public_key():
    """Return the committed Ed25519 public key, or None when unavailable."""
    try:
        public_key = PUBLIC_KEY_PATH.read_bytes()
    except OSError:
        logger.warning("openbox-release.pub is missing; release signatures cannot be verified")
        return None
    if len(public_key) != 32:
        logger.warning("openbox-release.pub has an invalid length; release signatures cannot be verified")
        return None
    return public_key


def verify_release_signature(update, artifact_digest, opener=urlopen):
    """Verify the release .sig against the committed public key.

    The known bootstrap placeholder and checksum-only releases are never
    installable.
    """
    sig_url = str(update.get("sig_url", "")).strip()
    if not sig_url:
        raise ValueError("The release is unsigned; refusing to install it.")
    if not sig_url.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The release signature URL is not a trusted OpenBox release asset.")
    public_key = _release_public_key()
    if public_key is None:
        raise ValueError("The committed OpenBox release public key is unavailable or invalid.")
    if public_key == PLACEHOLDER_PUBLIC_KEY:
        raise ValueError("The committed OpenBox release public key is still the placeholder.")
    signature = load_release_signature(sig_url, opener=opener)
    return verify_update_signature(update, artifact_digest, signature, public_key)


def verify_artifact(artifact, signature_file, public_key_file) -> str:
    """Verify a downloaded release artifact against its ``.sig`` and a raw key.

    This is the portable equivalent of ``scripts/verify_release.py``: the
    installer scripts verify with this so Windows never needs OpenSSL. Returns
    the hex SHA-256 digest that was verified.
    """
    artifact, signature_file, public_key_file = (Path(path) for path in (artifact, signature_file, public_key_file))
    payload = json.loads(signature_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("The release signature is invalid.")
    if payload.get("algorithm") != "ed25519" or payload.get("digest_algorithm") != "sha256":
        raise ValueError("The release signature does not use Ed25519 over SHA-256.")
    expected = str(payload.get("digest", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The release signature is missing a valid digest.")
    digest = hashlib.sha256()
    with artifact.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected != actual:
        raise ValueError("The artifact does not match the signed release digest.")
    try:
        signature = base64.b64decode(str(payload.get("signature", "")), validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("The release signature is missing a valid signature.") from error
    public_key = public_key_file.read_bytes()
    if len(public_key) != 32:
        raise ValueError("The release public key is invalid.")
    if public_key == PLACEHOLDER_PUBLIC_KEY:
        raise ValueError("The committed OpenBox release public key is still the placeholder.")
    if not _verify_ed25519(public_key, signature, bytes.fromhex(actual)):
        raise ValueError("The release signature verification failed.")
    return actual


def check_update(opener=urlopen):
    try:
        with github_request(RELEASE_API, opener=opener) as response:
            release = json.loads(read_limited(response, 8 * 1024 * 1024))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise ValueError(f"GitHub releases request failed ({error.code}): {detail or error.reason}") from error
    except URLError as error:
        raise ValueError(f"Could not reach GitHub releases: {error.reason}") from error

    if not isinstance(release, dict):
        raise ValueError("The GitHub releases payload is invalid.")
    version = str(release.get("tag_name", ""))
    urls, digests = parse_release_assets(release)
    artifact = urls.get(ASSET, "")
    checksum = digests.get(ASSET, "")
    checksum_url = urls.get(f"{ASSET}.sha256", "")
    sig_url = urls.get(SIGNATURE_ASSET, "")
    try:
        release_available = _version_key(version) > _version_key(VERSION)
    except ValueError:
        release_available = False
    if release_available and re.search(r"[-+]", version):
        # Never auto-update to a pre-release or build-suffixed tag.
        release_available = False
    if release_available and not artifact.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The release is missing verified OpenBox update assets.")
    if release_available and not checksum and not checksum_url:
        raise ValueError("The release is missing a SHA-256 checksum for the download.")
    if release_available and not sig_url:
        raise ValueError("The release is missing an Ed25519 signature.")
    if release_available and not sig_url.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The release signature URL is not a trusted OpenBox release asset.")
    return {
        "current": VERSION,
        "latest": version.lstrip("v"),
        "available": release_available,
        "notes": str(release.get("body", ""))[:4000],
        "artifact": artifact,
        "checksum": checksum,
        "checksum_url": checksum_url,
        "sig": bool(sig_url),
        "sig_url": sig_url,
        "page": str(release.get("html_url", "")),
    }


def install_update(update, destination=None, opener=urlopen):
    """Install a verified update through this platform's channel."""
    if IS_WINDOWS:
        return _install_update_windows(update, opener=opener)
    return _install_update_appimage(update, destination, opener=opener)


def _install_update_appimage(update, destination=None, opener=urlopen):
    # Resolve symlinks so the real AppImage is replaced, not the link.
    destination = Path(destination or os.environ.get("APPIMAGE", "")).expanduser().resolve()
    if not destination.is_file():
        raise ValueError("Automatic updates require the OpenBox AppImage.")
    if not update.get("available"):
        raise ValueError("OpenBox is already up to date.")
    appimage = str(update.get("artifact", "")).strip()
    if not appimage.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The update URLs are not trusted OpenBox release assets.")
    expected = resolve_update_checksum(update, opener=opener)
    # Verify the Ed25519 signature before anything is downloaded. The checksum
    # remains an independent corruption check during the download.
    verify_release_signature(update, expected, opener=opener)
    temporary = destination.with_name(f".{destination.name}.update")
    try:
        download_file(
            update["artifact"], temporary, max_bytes=2 * 1024 * 1024 * 1024,
            timeout=60, opener=opener, sha256=expected,
        )
        temporary.chmod(destination.stat().st_mode)
        backup = destination.with_name(f"{destination.stem}.previous{destination.suffix}")
        if backup.exists():
            backup.unlink()
        destination.replace(backup)
        try:
            temporary.replace(destination)
        except OSError:
            backup.replace(destination)
            raise
        fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {"installed": update["latest"], "backup": str(backup)}


def install_desktop_entry(appimage=None):
    """Install the platform's desktop integration for this copy of OpenBox."""
    if IS_WINDOWS:
        return _install_windows_entry(appimage)
    appimage = Path(appimage or os.environ.get("APPIMAGE", "")).expanduser()
    if not appimage.is_file():
        raise ValueError("Desktop integration requires the OpenBox AppImage.")
    executable = str(appimage)
    if "\n" in executable:
        raise ValueError("The AppImage path is not valid for a desktop entry.")
    executable = executable.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    # '%' starts a desktop-entry field code, so literal percent must double.
    executable = executable.replace("%", "%%")
    applications = Path.home() / ".local/share/applications"
    icons = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    icon = icons / "io.openbox.GameLauncher.svg"
    atomic_write_bytes(icon, (Path(__file__).parent / "openbox.svg").read_bytes(), mode=0o644)
    desktop = applications / "io.openbox.GameLauncher.desktop"
    atomic_write_text(desktop, (
        "[Desktop Entry]\n"
        "Name=OpenBox\n"
        "Comment=Local-first Linux game library and launcher\n"
        f'Exec="{executable}"\n'
        "Icon=io.openbox.GameLauncher\n"
        "Terminal=false\n"
        "Type=Application\n"
        "Categories=Game;Emulator;\n"
        "Keywords=games;launcher;emulator;rom;\n"
    ), mode=0o755)
    desktop.chmod(0o755)
    return str(desktop)


# ---------------------------------------------------------------------------
# Windows installation channel
# ---------------------------------------------------------------------------

WINDOWS_LAUNCHER = "openbox.cmd"


if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI

    def _install_root() -> Path:
        """The directory holding the running OpenBox tree."""
        return Path(__file__).resolve().parent

    def _installed_root() -> Path | None:
        """The portable install tree, or None when running from a checkout.

        Auto-updates only ever replace an installed copy, mirroring the
        ``APPIMAGE`` requirement on Linux; a working tree is never moved.
        """
        root = windows_install_dir()
        if root is None:
            return None
        share = root / "share" / "openbox"
        return share if (share / "web_app.py").is_file() else None

    def _powershell(script: str, *, detached: bool = False):
        """Run PowerShell through -EncodedCommand, so quoting cannot bite."""
        def _encode(text: str) -> str:
            return base64.b64encode(text.encode("utf-16-le")).decode("ascii")

        invocation = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached:
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            return subprocess.Popen(invocation + [_encode(script)], creationflags=flags, close_fds=True)
        # Windows PowerShell 5.1 encodes redirected stdout with the console
        # codepage, which mangles any non-ASCII path in an error message; force
        # UTF-8 so the capture below decodes exactly.
        script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n" + script
        return subprocess.run(
            invocation + [_encode(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=flags,
        )

    def _ps_quote(value) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _create_shortcut(launcher: Path, link: Path, icon: Path | None) -> None:
        script = [
            "$ErrorActionPreference = 'Stop'",
            "$shell = New-Object -ComObject WScript.Shell",
            f"$shortcut = $shell.CreateShortcut({_ps_quote(link)})",
            f"$shortcut.TargetPath = {_ps_quote(launcher)}",
            f"$shortcut.WorkingDirectory = {_ps_quote(launcher.parent)}",
            "$shortcut.Description = 'OpenBox game library'",
        ]
        if icon is not None:
            script.append(f"$shortcut.IconLocation = {_ps_quote(icon)}")
        script.append("$shortcut.Save()")
        result = _powershell("\r\n".join(script))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ValueError(f"Could not create the Start Menu shortcut: {detail or result.returncode}")

    def register_protocol(launcher: Path) -> None:
        """Register the ``openbox://`` URI scheme for the current user."""
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\openbox") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:OpenBox Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\openbox\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{launcher}" "%1"')

    def _install_windows_entry(launcher=None) -> str:
        launcher = Path(launcher or _install_root() / WINDOWS_LAUNCHER).expanduser().resolve()
        if not launcher.is_file():
            raise ValueError("Desktop integration requires the OpenBox launcher.")
        programs = start_menu_programs_dir()
        if programs is None:
            raise ValueError("The Start Menu folder is unavailable.")
        programs.mkdir(parents=True, exist_ok=True)
        link = programs / "OpenBox.lnk"
        icon = _install_root() / "openbox.ico"
        _create_shortcut(launcher, link, icon if icon.is_file() else None)
        register_protocol(launcher)
        return str(link)

    def _payload_root(directory: Path) -> Path:
        """The single top-level folder of an extracted archive, when it has one."""
        entries = [entry for entry in directory.iterdir() if not entry.name.startswith("__")]
        folders = [entry for entry in entries if entry.is_dir()]
        files = [entry for entry in entries if not entry.is_dir()]
        return folders[0] if len(folders) == 1 and not files else directory

    def _applier_script(pid: int, target: Path, staged: Path, scratch: Path) -> str:
        """PowerShell that swaps the staged tree in once *pid* has exited.

        A running ``.exe`` cannot be replaced in place, so the swap happens
        after the app exits: the old tree is kept as ``<target>.previous``.
        """
        previous = Path(f"{target}.previous")
        return "\r\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$targetPid = {int(pid)}",
            f"$target = {_ps_quote(target)}",
            f"$staged = {_ps_quote(staged)}",
            f"$scratch = {_ps_quote(scratch)}",
            f"$previous = {_ps_quote(previous)}",
            "$deadline = (Get-Date).AddMinutes(10)",
            "while (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) {",
            "    if ((Get-Date) -gt $deadline) { exit 1 }",
            "    Start-Sleep -Milliseconds 250",
            "}",
            "if (Test-Path -LiteralPath $previous) { Remove-Item -LiteralPath $previous -Recurse -Force }",
            "if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $previous }",
            "Move-Item -LiteralPath $staged -Destination $target",
            "Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue",
        ])

    def _install_update_windows(update, opener=urlopen) -> dict:
        target = _installed_root()
        if target is None:
            raise ValueError("Automatic updates require an installed copy of OpenBox.")
        if not update.get("available"):
            raise ValueError("OpenBox is already up to date.")
        archive_url = str(update.get("artifact", "")).strip()
        if not archive_url.startswith(TRUSTED_RELEASE_PREFIX):
            raise ValueError("The update URLs are not trusted OpenBox release assets.")
        expected = resolve_update_checksum(update, opener=opener)
        verify_release_signature(update, expected, opener=opener)

        scratch = Path(tempfile.mkdtemp(prefix=f".{target.name}.next-", dir=target.parent))
        try:
            archive = scratch / ASSET
            download_file(
                archive_url, archive, max_bytes=2 * 1024 * 1024 * 1024,
                timeout=60, opener=opener, sha256=expected,
            )
            payload = scratch / "payload"
            payload.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(payload)
            staged = _payload_root(payload)
            if not (staged / "web_app.py").is_file():
                raise ValueError("The update archive is not an OpenBox installation.")
            archive.unlink()
            _powershell(_applier_script(os.getpid(), target, staged, scratch), detached=True)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return {"installed": update["latest"], "backup": f"{target}.previous", "restart_required": True}


def _cli_verify(argv) -> int:
    if len(argv) != 3:
        print("usage: updates.py verify <artifact> <signature> <public-key>", file=sys.stderr)
        return 2
    try:
        digest = verify_artifact(*argv)
    except (OSError, ValueError) as error:
        # TEST-UPDATE-FAIL is the machine-readable refusal marker the
        # trust-chain tests grep for: a tampered, unsigned, or otherwise
        # unverifiable artifact must fail loudly, never silently.
        print(f"verification failed: {error}", file=sys.stderr)
        print("TEST-UPDATE-FAIL", file=sys.stderr)
        return 1
    print(digest)
    return 0


def main(argv=None):
    """``verify`` and ``install-desktop-entry`` are used by the installers."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify"]:
        return _cli_verify(argv[1:])
    if argv[:1] == ["install-desktop-entry"]:
        print(install_desktop_entry(argv[1] if len(argv) > 1 else None))
        return 0
    # RFC 8032 round-trip: malformed points must fail cleanly, not verify.
    # The canonical signer is exercised by test_release_signing.py; this
    # only proves the decoder rejects invalid and non-canonical points.
    zero_sig = bytes(64)
    try:
        _point_decompress((2).to_bytes(32, "little"))
        raise SystemExit("off-curve point accepted")
    except ValueError:
        pass
    try:
        _verify_ed25519(b"\xff" * 32, zero_sig, b"payload")
        raise SystemExit("out-of-range point verified")
    except ValueError:
        pass
    print("ed25519 decoder self-test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
