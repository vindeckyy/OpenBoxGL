#!/usr/bin/env bash
# OpenBox release pipeline: everything mechanical up to the human approval.
#
#   ./scripts/release.sh
#
# Runs: version sync -> make check gate -> AppImage build -> SBOM -> signing
# -> release notes draft. The final `gh release`
# publish is intentionally left to the maintainer.
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
python3 scripts/gen_sbom.py --version "$VERSION" --appdir "$temporary/OpenBox.AppDir" --out "OpenBox-$VERSION-$arch-sbom.json"

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

echo "== 6/6 release notes draft =="
CHANGELOG_FILE="CHANGELOG.md"
if [ -f "docs/CHANGELOG.md" ]; then
  CHANGELOG_FILE="docs/CHANGELOG.md"
fi
cat > "release-notes-$VERSION.md" <<NOTES
# OpenBox v$VERSION

$(sed -n '/^## Unreleased/,/^## \[/p' "$CHANGELOG_FILE" | sed '1d;$d')

## Verification

- \`make check\`: lint, compile, $({ ls test_*.py tests/test_*.py 2>/dev/null | wc -l; }) test files, coverage floors green.
- SBOM: \`OpenBox-$VERSION-$arch-sbom.json\` (CycloneDX 1.4)
- SHA-256: \`$(cut -d' ' -f1 "$appimage.sha256")\`
- Ed25519 signature: \`$appimage.sig\` (verified against openbox-release.pub)

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v$(git tag --sort=-v:refname | head -1 | sed 's/^v//')...v$VERSION
NOTES

echo
echo "Pipeline complete. Review release-notes-$VERSION.md, then run:"
echo "  gh release create v$VERSION $appimage $appimage.zsync $appimage.sha256 $appimage.sig openbox-release.pub scripts/install.sh OpenBox-$VERSION-$arch-sbom.json --notes-file release-notes-$VERSION.md"
