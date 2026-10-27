#!/usr/bin/env bash
# Install the signed OpenBox app bundle from a GitHub release.
#
# The bundle is ad-hoc signed, so the first launch needs one user action:
# System Settings > Privacy & Security > Open Anyway. The release signature
# is what this script verifies before it installs anything.
set -euo pipefail

REPO="vindeckyy/OpenBoxGL"
ASSET="OpenBox-macos.zip"
KEY_ASSET="openbox-release.pub"
SIG_ASSET="${ASSET}.sig"
VERIFIER_ASSET="verify_release.py"
DEST_DIR="${OPENBOX_INSTALL_DIR:-$HOME/Applications}"
# Bootstrap trust anchor for the committed production release key: the
# SHA-256 of openbox-release.pub, the same constant scripts/install.sh uses.
RELEASE_KEY_SHA256="39db9c52aa2e6e24b06ac845c29e0aeda60f3facf1ee30007913d7e93368d9ea"

umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TMP_DIR"' EXIT

say() { printf '\033[1;32m%s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }
fetch() {
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --output "$1" "$2"
}

[ "$(uname -s)" = "Darwin" ] || die "this installer installs the macOS app bundle."
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v shasum >/dev/null 2>&1 || die "shasum is required."
command -v ditto >/dev/null 2>&1 || die "ditto is required."
command -v python3 >/dev/null 2>&1 || die "python3 3.10 or newer is required to verify the release."

# Resolve the release tag, then require a stable semantic-version tag before
# it is used in any download URL. OPENBOX_RELEASE_TAG pins a manually
# reviewed tag.
if [[ -n "${OPENBOX_RELEASE_TAG:-}" ]]; then
  TAG="$OPENBOX_RELEASE_TAG"
else
  TAG="$(curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
    "https://api.github.com/repos/$REPO/releases/latest" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
fi
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$ ]] \
  || die "The release tag is not a valid semantic-version tag."

BASE="https://github.com/$REPO/releases/download/$TAG"
say "Found release $TAG"

say "Downloading signed release assets..."
fetch "$BASE/$ASSET" "$TMP_DIR/$ASSET" || die "app bundle download failed."
fetch "$BASE/$ASSET.sha256" "$TMP_DIR/$ASSET.sha256" || die "Could not fetch the SHA-256 checksum."
fetch "$BASE/$SIG_ASSET" "$TMP_DIR/$SIG_ASSET" || die "Could not fetch the Ed25519 signature."
fetch "$BASE/$KEY_ASSET" "$TMP_DIR/$KEY_ASSET" || die "Could not fetch the release public key."
fetch "$BASE/$VERIFIER_ASSET" "$TMP_DIR/$VERIFIER_ASSET" || die "Could not fetch the release verifier."

say "Verifying the release key, the bundle checksum, and the signature..."
actual_key_hash="$(shasum -a 256 "$TMP_DIR/$KEY_ASSET" | awk '{print $1}')"
[[ "$actual_key_hash" = "$RELEASE_KEY_SHA256" ]] \
  || die "The release public key does not match the pinned trust anchor."
( cd "$TMP_DIR" && shasum -a 256 -c "$ASSET.sha256" ) \
  || die "The app bundle checksum does not match."
python3 "$TMP_DIR/$VERIFIER_ASSET" --key "$TMP_DIR/$KEY_ASSET" \
  "$TMP_DIR/$ASSET" "$TMP_DIR/$SIG_ASSET" \
  || die "Ed25519 signature verification failed."

mkdir -p "$TMP_DIR/extract"
ditto -x -k "$TMP_DIR/$ASSET" "$TMP_DIR/extract" || die "The app bundle could not be extracted."
[ -d "$TMP_DIR/extract/OpenBox.app" ] || die "The archive does not contain OpenBox.app."

mkdir -p "$DEST_DIR"
if [ -e "$DEST_DIR/OpenBox.app" ] && [ "${1:-}" != "--replace" ]; then
  die "$DEST_DIR/OpenBox.app already exists. Re-run with --replace to replace it."
fi
rm -rf "$DEST_DIR/OpenBox.app"
mv "$TMP_DIR/extract/OpenBox.app" "$DEST_DIR/OpenBox.app"
say "Installed to $DEST_DIR/OpenBox.app"

mkdir -p "$HOME/.local/bin"
ln -sfn "$DEST_DIR/OpenBox.app/Contents/Resources/bin/openbox" "$HOME/.local/bin/openbox"
say "Linked $HOME/.local/bin/openbox"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) say "Add $HOME/.local/bin to your PATH to run 'openbox' from a terminal." ;;
esac

say "Done. Launch OpenBox from the Applications folder."
say "The bundle is not notarized: the first launch asks you to allow it in"
say "System Settings > Privacy & Security > Open Anyway."
