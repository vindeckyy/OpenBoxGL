#!/usr/bin/env python3
"""Sign an OpenBox release artifact with an Ed25519 key.

Produces <artifact>.sig containing the base64 signature of the artifact's
SHA-256 digest. Verification lives in scripts/verify_release.py and, once a
trusted key ships, in the app's updater.

Usage:
  python3 scripts/sign_release.py --key /path/to/ed25519.priv \
    --public-key-out /path/to/openbox-release.pub OpenBox-x86_64.AppImage
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as error:  # pragma: no cover - signing is a maintainer-only step
    print("signing requires the pinned development tools: make dev-venv", file=sys.stderr)
    raise SystemExit(2) from error


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def load_private_key(path):
    """Load raw, hexadecimal, or base64-encoded Ed25519 private key bytes."""
    raw = Path(path).read_bytes()
    if len(raw) == 32:
        return raw
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValueError("The Ed25519 private key must contain exactly 32 bytes.") from error
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        decoded = bytes.fromhex(text)
    else:
        try:
            decoded = base64.b64decode(text, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("The Ed25519 private key must be raw, hexadecimal, or base64 encoded.") from error
    if len(decoded) != 32:
        raise ValueError("The Ed25519 private key must decode to exactly 32 bytes.")
    return decoded


def sign(artifact, private_key_path, out=None, public_key_out=None):
    artifact = Path(artifact)
    private_key = Ed25519PrivateKey.from_private_bytes(load_private_key(private_key_path))
    artifact_digest = digest_file(artifact)
    signature = private_key.sign(artifact_digest)
    payload = {
        "algorithm": "ed25519",
        "artifact": artifact.name,
        "digest_algorithm": "sha256",
        "digest": artifact_digest.hex(),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    out_path = Path(out) if out else artifact.with_suffix(artifact.suffix + ".sig")
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    if public_key_out:
        public_path = Path(public_key_out)
        public_path.write_bytes(public_bytes)
        print(f"wrote {out_path} and {public_path}")
    else:
        print(f"wrote {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--out")
    parser.add_argument("--public-key-out")
    parser.add_argument("artifact")
    args = parser.parse_args()
    sign(args.artifact, args.key, args.out, args.public_key_out)


if __name__ == "__main__":
    main()
