#!/usr/bin/env bash
# Install the latest OpenBoxGL AppImage.
# Downloads from GitHub Releases, verifies SHA-256, installs to ~/.local/bin.
set -euo pipefail

REPO="vindeckyy/OpenBoxGL"
ASSET="OpenBox-x86_64.AppImage"
DEST_DIR="${OPENBOX_INSTALL_DIR:-$HOME/.local/bin}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

say() { printf '\033[1;32m%s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# Resolve the latest release tag from the GitHub API.
say "Looking up the latest release..."
TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')" \
  || die "Could not resolve the latest release. Check your network connection."

BASE="https://github.com/$REPO/releases/download/$TAG"
say "Found release $TAG"

# Download the AppImage and its checksum sidecar.
say "Downloading $ASSET ($TAG)..."
curl -fL --progress-bar -o "$TMP_DIR/$ASSET" "$BASE/$ASSET" \
  || die "Download failed."
curl -fsSL -o "$TMP_DIR/$ASSET.sha256" "$BASE/$ASSET.sha256" \
  || die "Could not fetch the SHA-256 checksum."

# Verify the checksum.
say "Verifying SHA-256..."
(
  cd "$TMP_DIR"
  sha256sum -c "$ASSET.sha256" >/dev/null
) || die "Checksum mismatch — the download is corrupt or the release was tampered with. Aborting."

mkdir -p "$DEST_DIR"
chmod +x "$TMP_DIR/$ASSET"
mv "$TMP_DIR/$ASSET" "$DEST_DIR/$ASSET"
say "Installed to $DEST_DIR/$ASSET"

# Symlink a friendly launcher name.
ln -sf "$DEST_DIR/$ASSET" "$DEST_DIR/openbox"
say "Linked $DEST_DIR/openbox"

# PATH hint.
if [[ ":$PATH:" != *":$DEST_DIR:"* ]]; then
  say "Note: $DEST_DIR is not on your PATH. Add it with:"
  say "  echo 'export PATH=\"\$PATH:$DEST_DIR\"' >> ~/.bashrc && source ~/.bashrc"
fi

if [[ "${1:-}" == "--run" ]]; then
  say "Launching OpenBox..."
  exec "$DEST_DIR/$ASSET" "${@:2}"
fi

say "Done. Run 'openbox' (or $DEST_DIR/$ASSET) to start OpenBox."
