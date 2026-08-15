import hashlib
import io
import json
import tempfile
from pathlib import Path

from updates import (
    ASSET,
    RELEASE_API,
    TRUSTED_RELEASE_PREFIX,
    VERSION,
    check_update,
    install_update,
    load_checksum_file,
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
        ],
    }
    def opener(request, timeout=0):
        url = request.full_url
        if url.endswith(".sha256"):
            return Response(f"{digest}  {ASSET}\n".encode())
        if url.endswith(ASSET):
            return Response(payload)
        return Response(json.dumps(release).encode())
    update = check_update(opener)
    assert update["available"] and update["latest"] == latest
    assert update["checksum"] == digest
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / ASSET
        destination.write_bytes(b"old appimage")
        result = install_update(update, destination, opener)
        assert destination.read_bytes() == payload
        assert Path(result["backup"]).read_bytes() == b"old appimage"
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
    print("update self-test: ok")


if __name__ == "__main__":
    main()
