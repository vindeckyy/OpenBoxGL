#!/usr/bin/env python3
"""Sign an OpenBox release artifact with an Ed25519 key.

Produces <artifact>.sig containing the base64 signature of the artifact's
SHA-256 digest. Verification lives in scripts/verify_release.py and, once a
trusted key ships, in the app's updater.

Usage:
  python3 scripts/sign_release.py --key /path/to/ed25519.priv OpenBox-x86_64.AppImage
"""

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as error:  # pragma: no cover - signing is a maintainer-only step
    print("signing requires the 'cryptography' package: pip install cryptography", file=sys.stderr)
    raise SystemExit(2) from error


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def sign(artifact, private_key_path, out=None):
    artifact = Path(artifact)
    key_path = Path(private_key_path)
    private_key = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
    signature = private_key.sign(digest_file(artifact))
    payload = {
        "algorithm": "ed25519",
        "artifact": artifact.name,
        "digest_algorithm": "sha256",
        "digest": digest_file(artifact).hex(),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    out_path = Path(out) if out else artifact.with_suffix(artifact.suffix + ".sig")
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    public_path = Path("openbox-release.pub")
    public_path.write_bytes(public_bytes)
    print(f"wrote {out_path} and {public_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--out")
    parser.add_argument("artifact")
    args = parser.parse_args()
    sign(args.artifact, args.key, args.out)


if __name__ == "__main__":
    main()
