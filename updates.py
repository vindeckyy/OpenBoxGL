"""Verified GitHub release updates for the OpenBox AppImage."""

import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend_io import atomic_write_bytes, atomic_write_text, download_file, read_limited

logger = logging.getLogger("openbox")

VERSION = "1.2.0"
RELEASE_API = "https://api.github.com/repos/vindeckyy/OpenBoxGL/releases/latest"
ASSET = "OpenBox-x86_64.AppImage"
TRUSTED_RELEASE_PREFIX = "https://github.com/vindeckyy/OpenBoxGL/releases/download/"

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
    # Reject small-order points (identity, order 2, order 4): they are
    # never valid verification keys for this application.
    if y in (0, 1, p - 1):
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
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("digest", "")).lower()):
        raise ValueError("The release signature is missing a valid digest.")
    try:
        signature = base64.b64decode(str(payload.get("signature", "")), validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("The release signature is missing a valid signature.") from error
    if len(signature) != 64:
        raise ValueError("The release signature has an invalid length.")
    return {"digest": payload["digest"].lower(), "signature": signature}


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


def check_update(opener=urlopen):
    try:
        with github_request(RELEASE_API, opener=opener) as response:
            release = json.loads(read_limited(response, 8 * 1024 * 1024))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise ValueError(f"GitHub releases request failed ({error.code}): {detail or error.reason}") from error
    except URLError as error:
        raise ValueError(f"Could not reach GitHub releases: {error.reason}") from error

    version = str(release.get("tag_name", ""))
    urls, digests = parse_release_assets(release)
    appimage = urls.get(ASSET, "")
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
    if release_available and not appimage.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The release is missing verified OpenBox update assets.")
    if release_available and not checksum and not checksum_url:
        raise ValueError("The release is missing a SHA-256 checksum for the AppImage.")
    if release_available and not sig_url:
        raise ValueError("The release is missing an Ed25519 signature for the AppImage.")
    if release_available and not sig_url.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The release signature URL is not a trusted OpenBox release asset.")
    return {
        "current": VERSION,
        "latest": version.lstrip("v"),
        "available": release_available,
        "notes": str(release.get("body", ""))[:4000],
        "appimage": appimage,
        "checksum": checksum,
        "checksum_url": checksum_url,
        "sig": bool(sig_url),
        "sig_url": sig_url,
        "page": str(release.get("html_url", "")),
    }


def install_update(update, destination=None, opener=urlopen):
    destination = Path(destination or os.environ.get("APPIMAGE", "")).expanduser()
    if not destination.is_file():
        raise ValueError("Automatic updates require the OpenBox AppImage.")
    if not update.get("available"):
        raise ValueError("OpenBox is already up to date.")
    appimage = str(update.get("appimage", "")).strip()
    if not appimage.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The update URLs are not trusted OpenBox release assets.")
    expected = resolve_update_checksum(update, opener=opener)
    # Verify the Ed25519 signature before anything is downloaded. The checksum
    # remains an independent corruption check during the download.
    verify_release_signature(update, expected, opener=opener)
    temporary = destination.with_name(f".{destination.name}.update")
    try:
        download_file(
            update["appimage"], temporary, max_bytes=2 * 1024 * 1024 * 1024,
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
    finally:
        temporary.unlink(missing_ok=True)
    return {"installed": update["latest"], "backup": str(backup)}


def install_desktop_entry(appimage=None):
    appimage = Path(appimage or os.environ.get("APPIMAGE", "")).expanduser()
    if not appimage.is_file():
        raise ValueError("Desktop integration requires the OpenBox AppImage.")
    executable = str(appimage)
    if "\n" in executable:
        raise ValueError("The AppImage path is not valid for a desktop entry.")
    executable = executable.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
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


def main():
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


if __name__ == "__main__":
    main()
