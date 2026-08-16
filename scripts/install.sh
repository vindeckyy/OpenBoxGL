#!/usr/bin/env bash
# Install a signed OpenBoxGL AppImage from a GitHub release.
set -euo pipefail

REPO="vindeckyy/OpenBoxGL"
ASSET="OpenBox-x86_64.AppImage"
KEY_ASSET="openbox-release.pub"
SIG_ASSET="${ASSET}.sig"
DEST_DIR="${OPENBOX_INSTALL_DIR:-$HOME/.local/bin}"
# Bootstrap trust anchor for the committed production release key.
RELEASE_KEY_SHA256="33135a3b4019c3d22d66d4b14e076824291f0ebbaf52d91bac9008a580ec00d9"

umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TMP_DIR"' EXIT

say() { printf '\033[1;32m%s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }
fetch() {
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --output "$2" "$1"
}

command -v curl >/dev/null 2>&1 || die "curl is required."
command -v python3 >/dev/null 2>&1 || die "python3 is required."
command -v openssl >/dev/null 2>&1 || die "openssl 3 is required for Ed25519 verification."
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required."

# Resolve a release tag, then require a stable semantic-version tag before it
# is used in any download URL. OPENBOX_RELEASE_TAG pins a manually reviewed tag.
if [[ -n "${OPENBOX_RELEASE_TAG:-}" ]]; then
  TAG="$OPENBOX_RELEASE_TAG"
else
  say "Looking up the latest release..."
  TAG="$(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "https://api.github.com/repos/$REPO/releases/latest" \
    | python3 -c 'import json,sys; value=json.load(sys.stdin).get("tag_name", ""); print(value if isinstance(value, str) else "")')" \
    || die "Could not resolve the latest release. Check your network connection."
fi
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$ ]] \
  || die "The release tag is not a valid semantic-version tag."

BASE="https://github.com/$REPO/releases/download/$TAG"
say "Found release $TAG"

say "Downloading signed release assets..."
fetch "$BASE/$ASSET" "$TMP_DIR/$ASSET" || die "AppImage download failed."
fetch "$BASE/$ASSET.sha256" "$TMP_DIR/$ASSET.sha256" || die "Could not fetch the SHA-256 checksum."
fetch "$BASE/$SIG_ASSET" "$TMP_DIR/$SIG_ASSET" || die "Could not fetch the Ed25519 signature."
fetch "$BASE/$KEY_ASSET" "$TMP_DIR/$KEY_ASSET" || die "Could not fetch the release public key."

say "Verifying the release key and AppImage checksum..."
actual_key_hash="$(sha256sum "$TMP_DIR/$KEY_ASSET" | awk '{print $1}')"
[[ "$actual_key_hash" == "$RELEASE_KEY_SHA256" ]] \
  || die "The release public key does not match the pinned trust anchor."
python3 - "$TMP_DIR/$ASSET" "$TMP_DIR/$ASSET.sha256" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

artifact, sidecar = map(Path, sys.argv[1:])
parts = sidecar.read_text(encoding="utf-8").split()
if not parts or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
    raise SystemExit("invalid SHA-256 sidecar")
digest = hashlib.sha256()
with artifact.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != parts[0].lower():
    raise SystemExit("AppImage checksum mismatch")
PY

# The signer signs the raw SHA-256 digest. Parse the JSON contract and make
# OpenSSL verify that digest with the pinned Ed25519 public key.
python3 - "$TMP_DIR/$ASSET" "$TMP_DIR/$SIG_ASSET" "$TMP_DIR/digest.bin" "$TMP_DIR/signature.bin" <<'PY'
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

artifact, signature_file, digest_file, raw_signature_file = map(Path, sys.argv[1:])
payload = json.loads(signature_file.read_text(encoding="utf-8"))
if payload.get("algorithm") != "ed25519" or payload.get("digest_algorithm") != "sha256":
    raise SystemExit("unsupported release signature")
expected = str(payload.get("digest", "")).lower()
if not re.fullmatch(r"[0-9a-f]{64}", expected):
    raise SystemExit("invalid release digest")
digest = hashlib.sha256()
with artifact.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if expected != actual:
    raise SystemExit("release signature digest mismatch")
try:
    raw_signature = base64.b64decode(str(payload["signature"]), validate=True)
except (KeyError, TypeError, ValueError) as error:
    raise SystemExit("invalid release signature encoding") from error
if len(raw_signature) != 64:
    raise SystemExit("invalid release signature length")
digest_file.write_bytes(bytes.fromhex(actual))
raw_signature_file.write_bytes(raw_signature)
PY
python3 - "$TMP_DIR/$KEY_ASSET" "$TMP_DIR/public.der" <<'PY'
import sys
from pathlib import Path

key, output = map(Path, sys.argv[1:])
raw = key.read_bytes()
if len(raw) != 32:
    raise SystemExit("invalid Ed25519 public key length")
# SubjectPublicKeyInfo wrapper for id-Ed25519, RFC 8410.
output.write_bytes(bytes.fromhex("302a300506032b6570032100") + raw)
PY
openssl pkeyutl -verify -rawin -pubin \
  -inkey "$TMP_DIR/public.der" \
  -in "$TMP_DIR/digest.bin" \
  -sigfile "$TMP_DIR/signature.bin" >/dev/null \
  || die "Ed25519 signature verification failed."

mkdir -p "$DEST_DIR"
chmod +x "$TMP_DIR/$ASSET"
mv -f "$TMP_DIR/$ASSET" "$DEST_DIR/$ASSET"
say "Installed to $DEST_DIR/$ASSET"

ln -sfn "$DEST_DIR/$ASSET" "$DEST_DIR/openbox"
say "Linked $DEST_DIR/openbox"

if [[ ":$PATH:" != *":$DEST_DIR:"* ]]; then
  say "Note: $DEST_DIR is not on your PATH. Add it with:"
  say "  echo 'export PATH=\"\$PATH:$DEST_DIR\"' >> ~/.bashrc && source ~/.bashrc"
fi

if [[ "${1:-}" == "--run" ]]; then
  say "Launching OpenBox..."
  exec "$DEST_DIR/$ASSET" "${@:2}"
fi

say "Done. Run 'openbox' (or $DEST_DIR/$ASSET) to start OpenBox."
