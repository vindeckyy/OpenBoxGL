import hashlib
import io
import json
import tempfile
from pathlib import Path

from updates import ASSET, VERSION, check_update, install_update, version_tuple


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
    latest = next_patch_version(VERSION)
    tag = f"v{latest}"
    payload = b"new appimage"
    digest = hashlib.sha256(payload).hexdigest()
    release = {
        "tag_name": tag,
        "html_url": f"https://github.com/vindeckyy/OpenBox/releases/tag/{tag}",
        "assets": [
            {"name": ASSET, "browser_download_url": f"https://github.com/vindeckyy/OpenBox/releases/download/{tag}/{ASSET}"},
            {"name": f"{ASSET}.sha256", "browser_download_url": f"https://github.com/vindeckyy/OpenBox/releases/download/{tag}/{ASSET}.sha256"},
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
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / ASSET
        destination.write_bytes(b"old appimage")
        result = install_update(update, destination, opener)
        assert destination.read_bytes() == payload
        assert Path(result["backup"]).read_bytes() == b"old appimage"
    print("update self-test: ok")


if __name__ == "__main__":
    main()
