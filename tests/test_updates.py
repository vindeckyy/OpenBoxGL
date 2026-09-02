import base64
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from updates import (
    ASSET,
    RELEASE_API,
    TRUSTED_RELEASE_PREFIX,
    VERSION,
    check_update,
    install_update,
    load_checksum_file,
    verify_release_signature,
    version_tuple,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def next_patch_version(value):
    major, minor, patch = version_tuple(value)
    return f"{major}.{minor}.{patch + 1}"


def main():
    assert version_tuple("v1.2.3") > version_tuple("1.2.2")
    assert RELEASE_API == "https://api.github.com/repos/vindeckyy/OpenBoxGL/releases/latest"
    assert TRUSTED_RELEASE_PREFIX == "https://github.com/vindeckyy/OpenBoxGL/releases/download/"
    latest = next_patch_version(VERSION)
    tag = f"v{latest}"
    payload = b"new appimage"
    digest = hashlib.sha256(payload).hexdigest()
    public_key = bytes.fromhex("03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
    signature = base64.b64decode("53/D1Z5YXcVxj9ZgLlTSt/jkeX9MDO53qddaj7bh64byCG1YW1/oTIbdiO8tNmoO7WaHkjkmM5SbYWusjIp6Dg==")
    signature_payload = {
        "algorithm": "ed25519",
        "artifact": ASSET,
        "digest_algorithm": "sha256",
        "digest": digest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    release = {
        "tag_name": tag,
        "html_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/tag/{tag}",
        "assets": [
            {
                "name": ASSET,
                "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/{ASSET}",
                "digest": f"sha256:{digest}",
            },
            {"name": f"{ASSET}.sha256", "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/{ASSET}.sha256"},
            {"name": f"{ASSET}.sig", "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/{ASSET}.sig"},
        ],
    }
    def opener(request, timeout=0):
        url = request.full_url
        if url.endswith(".sha256"):
            return Response(f"{digest}  {ASSET}\n".encode())
        if url.endswith(".sig"):
            return Response(json.dumps(signature_payload).encode())
        if url.endswith(ASSET):
            return Response(payload)
        return Response(json.dumps(release).encode())
    update = check_update(opener)
    assert update["available"] and update["latest"] == latest
    assert update["checksum"] == digest
    assert update["sig"] is True
    with mock.patch("updates._release_public_key", return_value=public_key):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ASSET
            destination.write_bytes(b"old appimage")
            result = install_update(update, destination, opener)
            assert destination.read_bytes() == payload
            assert Path(result["backup"]).read_bytes() == b"old appimage"
    try:
        unsigned = dict(release, assets=[asset for asset in release["assets"] if not asset["name"].endswith(".sig")])
        check_update(lambda request, timeout=0: Response(json.dumps(unsigned).encode()))
        raise AssertionError("unsigned release should fail closed")
    except ValueError as error:
        assert "signature" in str(error).casefold()
    from updates import PLACEHOLDER_PUBLIC_KEY
    with mock.patch("updates._release_public_key", return_value=PLACEHOLDER_PUBLIC_KEY):
        try:
            verify_release_signature(update, digest, opener)
            raise AssertionError("placeholder release key should fail closed")
        except ValueError as error:
            assert "placeholder" in str(error).casefold()
    try:
        load_checksum_file(
            f"{TRUSTED_RELEASE_PREFIX}{tag}/{ASSET}.sha256",
            opener=lambda request, timeout=0: Response(b" \n"),
        )
        raise AssertionError("empty checksum should fail")
    except ValueError as error:
        assert "checksum" in str(error).casefold()

    # A pre-release tag parses without raising and is not "available".
    from updates import _version_key
    assert _version_key("1.2.3-rc1") < _version_key("1.2.3")
    assert _version_key("1.2.3+build") < _version_key("1.2.3")
    pre_release = dict(release, tag_name=f"v{latest}-rc1")
    def pre_opener(request, timeout=0):
        return Response(json.dumps(pre_release).encode())
    pre_update = check_update(pre_opener)
    assert pre_update["available"] is False

    # Malformed Ed25519 points must be rejected, never verified.
    from updates import _point_decompress, _verify_ed25519
    p = 2 ** 255 - 19
    for bad in (
        (p).to_bytes(32, "little"),          # y == p: out of range
        (2).to_bytes(32, "little"),          # not on the curve
        bytes(32),                           # identity / small order
        (1).to_bytes(32, "little"),          # order-2 point
        (p - 1).to_bytes(32, "little"),      # order-2 point
    ):
        try:
            _point_decompress(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid point accepted")
    for bad in (bytes(32), (1).to_bytes(32, "little"), (p).to_bytes(32, "little")):
        try:
            result = _verify_ed25519(bad, bytes(64), b"msg")
        except ValueError:
            result = False
        assert result is False

    # Architecture-aware updater: ASSET follows the host arch, and a release
    # without the matching-arch artifact is refused (ADR 0024).
    from updates import _arch_asset, _current_arch
    assert _arch_asset("x86_64") == "OpenBox-x86_64.AppImage"
    assert _arch_asset("aarch64") == "OpenBox-aarch64.AppImage"
    assert _current_arch("x86_64") == "x86_64"
    assert _current_arch("amd64") == "x86_64"
    assert _current_arch("aarch64") == "aarch64"
    assert _current_arch("arm64") == "aarch64"
    # ASSET is bound at import time from the host arch, so an aarch64 host is
    # simulated by patching ASSET/SIGNATURE_ASSET directly. A release shipping
    # only the x86_64 AppImage must then be refused (matching asset absent).
    x86_only_release = {
        "tag_name": tag,
        "html_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/tag/{tag}",
        "assets": [
            {
                "name": "OpenBox-x86_64.AppImage",
                "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/OpenBox-x86_64.AppImage",
                "digest": f"sha256:{digest}",
            },
        ],
    }
    with mock.patch("updates.ASSET", "OpenBox-aarch64.AppImage"), \
         mock.patch("updates.SIGNATURE_ASSET", "OpenBox-aarch64.AppImage.sig"):
        try:
            check_update(lambda request, timeout=0: Response(json.dumps(x86_only_release).encode()))
            raise AssertionError("aarch64 host should refuse an x86_64-only release")
        except ValueError as error:
            assert "asset" in str(error).casefold()
    print("update self-test: ok")


if __name__ == "__main__":
    main()
