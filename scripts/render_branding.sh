#!/usr/bin/env bash
# Development-only: regenerate application and README logos from the source SVG.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v magick >/dev/null || { echo "Install ImageMagick to render branding assets." >&2; exit 1; }
cp "$repo_root/assets/OpenBoxLogo.svg" "$repo_root/openbox.svg"
magick -background none "$repo_root/assets/OpenBoxLogo.svg" -resize 512x512 -depth 8 "$repo_root/assets/openbox-logo.png"
magick -background none "$repo_root/assets/OpenBoxLogo.svg" -depth 8 "$repo_root/assets/OpenBoxGL.png"
