#!/usr/bin/env python3
"""Verify an OpenBox release artifact against its .sig file.

Uses only the standard library: Ed25519 verification over SHA-256.

Usage:
  python3 scripts/verify_release.py --key openbox-release.pub OpenBox-x86_64.AppImage OpenBox-x86_64.AppImage.sig
"""

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path


def _point_decompress(public_bytes):
    """Decompress an Ed25519 public key to affine coordinates (RFC 8032)."""
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


def verify_ed25519(public_bytes, signature, message):
    """RFC 8032 Ed25519 verification with stdlib big ints."""
    p = 2 ** 255 - 19
    L = 2 ** 252 + 27742317777372353535851937790883648493
    # Ed25519 base point B
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

    # RFC 8032: [S]B must equal R + [k]A.
    kA = point_mul(k, A)
    if kA is None:
        kA = (0, 1)
    return point_mul(s, (Bx, By)) == point_add(R, kA)


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("artifact")
    parser.add_argument("signature_file")
    args = parser.parse_args()

    public_bytes = Path(args.key).read_bytes()
    if len(public_bytes) != 32:
        print("invalid public key length", file=sys.stderr)
        return 1

    payload = json.loads(Path(args.signature_file).read_text())
    if payload.get("algorithm") != "ed25519":
        print(f"unsupported algorithm: {payload.get('algorithm')}", file=sys.stderr)
        return 1

    artifact_digest = digest_file(Path(args.artifact))
    if payload.get("digest") != artifact_digest.hex():
        print("digest mismatch: the artifact changed after signing", file=sys.stderr)
        return 1

    signature = base64.b64decode(payload["signature"])
    if not verify_ed25519(public_bytes, signature, artifact_digest):
        print("signature verification failed", file=sys.stderr)
        return 1

    print(f"OK: {Path(args.artifact).name} matches {Path(args.signature_file).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
