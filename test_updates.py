import hashlib
import io
import json
import tempfile
from pathlib import Path

from updates import ASSET, check_update, install_update, version_tuple


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    assert version_tuple("v1.2.3") > version_tuple("1.2.2")
    payload = b"new appimage"
    digest = hashlib.sha256(payload).hexdigest()
    release = {
        "tag_name":"v0.3.0",
        "html_url":"https://github.com/vindeckyy/OpenBox/releases/tag/v0.3.0",
        "assets":[
            {"name":ASSET, "browser_download_url":f"https://github.com/vindeckyy/OpenBox/releases/download/v0.3.0/{ASSET}"},
            {"name":f"{ASSET}.sha256", "browser_download_url":f"https://github.com/vindeckyy/OpenBox/releases/download/v0.3.0/{ASSET}.sha256"},
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
    assert update["available"] and update["latest"] == "0.3.0"
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / ASSET
        destination.write_bytes(b"old appimage")
        result = install_update(update, destination, opener)
        assert destination.read_bytes() == payload
        assert Path(result["backup"]).read_bytes() == b"old appimage"
    print("update self-test: ok")


if __name__ == "__main__":
    main()
