#!/usr/bin/env bash
# OpenBox release pipeline: local artifact preflight up to the maintainer tag.
#
#   ./scripts/release.sh
#
# Runs: version sync -> make check gate -> AppImage build -> SBOM -> signing
# -> release notes preparation. Pushing an annotated v* tag starts the GitHub
# AppImage and Flatpak workflows; those workflows publish the shared release.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() { echo "release.sh: $*" >&2; exit 1; }

# The AppImage bundles the host interpreter, so a release artifact follows the
# build host arch. OPENBOX_ARCH overrides detection (CI cross-builds set it).
arch="${OPENBOX_ARCH:-$(uname -m)}"
case "$arch" in
  x86_64|amd64) arch="x86_64" ;;
  aarch64|arm64) arch="aarch64" ;;
  *) fail "unsupported architecture: $arch" ;;
esac
appimage="OpenBox-$arch.AppImage"

temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
umask 077

echo "== 1/6 version sync =="
python3 scripts/check_version_sync.py

echo "== 2/6 verification gate =="
make check

echo "== 3/6 AppImage build ($arch) =="
OPENBOX_APPDIR="$temporary/OpenBox.AppDir" bash build_appimage.sh "$PWD/$appimage"
[ -f "$appimage" ] || fail "AppImage missing after build"

VERSION="$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("updates.py").read(), re.M).group(1))')"

echo "== 4/6 SBOM =="
# x86_64 keeps the legacy unprefixed SBOM name matching prior releases;
# aarch64 gets an arch-suffixed name so the two are distinct (ADR 0024).
if [ "$arch" = "x86_64" ]; then
  sbom_name="OpenBox-$VERSION-sbom.json"
else
  sbom_name="OpenBox-$VERSION-$arch-sbom.json"
fi
python3 scripts/gen_sbom.py --version "$VERSION" --appdir "$temporary/OpenBox.AppDir" --out "$sbom_name"

echo "== 5/6 checksum + signature =="
sha256sum "$appimage" | tee "$appimage.sha256"
signing_key="${OPENBOX_SIGNING_KEY:-}"
[ -n "$signing_key" ] || fail "OPENBOX_SIGNING_KEY is required; refusing to create an unsigned release"
if [ -f "$signing_key" ]; then
  signing_key_path="$signing_key"
else
  signing_key_path="$temporary/openbox-release.key"
  printf '%s' "$signing_key" > "$signing_key_path"
fi
unset OPENBOX_SIGNING_KEY
generated_public_key="$temporary/openbox-release.pub"
python3 scripts/sign_release.py \
  --key "$signing_key_path" \
  --public-key-out "$generated_public_key" \
  --out "$appimage.sig" \
  "$appimage"
cmp -s "$generated_public_key" openbox-release.pub \
  || fail "signing key does not match the committed openbox-release.pub"
python3 scripts/verify_release.py --key openbox-release.pub "$appimage" "$appimage.sig"

echo "== 6/6 release notes preparation =="
CHANGELOG_FILE="CHANGELOG.md"
if [ -f "docs/CHANGELOG.md" ]; then
  CHANGELOG_FILE="docs/CHANGELOG.md"
fi
notes_file="release-notes-$VERSION.md"
if [ -f "docs/RELEASE_NOTES.md" ] && grep -q "^# OpenBox $VERSION" docs/RELEASE_NOTES.md; then
  cp docs/RELEASE_NOTES.md "$notes_file"
else
  cat > "$notes_file" <<NOTES
# OpenBox $VERSION

$(sed -n '/^## Unreleased/,/^## \[/p' "$CHANGELOG_FILE" | sed '1d;$d')
NOTES
fi

cat >> "$notes_file" <<NOTES

## Verification

- \`make check\`: lint, compile, $(find . -type f -name 'test_*.py' -not -path './.git/*' -not -path './build/*' -not -path './.venv-dev/*' | wc -l | tr -d ' ') test files, and coverage floors green.
- SBOM: \`$sbom_name\` (CycloneDX 1.4)
- SHA-256: \`$(cut -d' ' -f1 "$appimage.sha256")\`
- Ed25519 signature: \`$appimage.sig\` (verified against openbox-release.pub)
NOTES

if ! grep -q '^\*\*Full Changelog\*\*' "$notes_file"; then
  previous_tag="$(git tag --sort=-v:refname | awk -v current="v$VERSION" '$0 != current { print; exit }')"
  if [ -n "$previous_tag" ]; then
    echo "" >> "$notes_file"
    echo "**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/$previous_tag...v$VERSION" >> "$notes_file"
  fi
fi

echo
echo "Pipeline complete. Review $notes_file, push the commit, then run:"
echo "  git tag -a v$VERSION -m \"OpenBox $VERSION\""
echo "  git push origin v$VERSION"
echo "The tag-triggered AppImage and Flatpak workflows publish the shared GitHub Release."
