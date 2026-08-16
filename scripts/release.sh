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

temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
umask 077

echo "== 1/6 version sync =="
python3 scripts/check_version_sync.py

echo "== 2/6 verification gate =="
make check

echo "== 3/6 AppImage build =="
OPENBOX_APPDIR="$temporary/OpenBox.AppDir" bash build_appimage.sh "$PWD/OpenBox-x86_64.AppImage"
[ -f OpenBox-x86_64.AppImage ] || fail "AppImage missing after build"

VERSION="$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("updates.py").read(), re.M).group(1))')"

echo "== 4/6 SBOM =="
python3 scripts/gen_sbom.py --version "$VERSION" --appdir "$temporary/OpenBox.AppDir" --out "OpenBox-$VERSION-sbom.json"

echo "== 5/6 checksum + signature =="
sha256sum OpenBox-x86_64.AppImage | tee OpenBox-x86_64.AppImage.sha256
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
  --out "OpenBox-x86_64.AppImage.sig" \
  OpenBox-x86_64.AppImage
cmp -s "$generated_public_key" openbox-release.pub \
  || fail "signing key does not match the committed openbox-release.pub"
python3 scripts/verify_release.py --key openbox-release.pub OpenBox-x86_64.AppImage OpenBox-x86_64.AppImage.sig

echo "== 6/6 release notes draft =="
cat > "release-notes-$VERSION.md" <<NOTES
# OpenBox v$VERSION

$(sed -n '/^## Unreleased/,/^## \[/p' CHANGELOG.md | sed '1d;$d')

## Verification

- \`make check\`: lint, compile, $({ ls test_*.py | wc -l; }) test files, coverage floors green.
- SBOM: \`OpenBox-$VERSION-sbom.json\` (CycloneDX 1.4)
- SHA-256: \`$(cut -d' ' -f1 OpenBox-x86_64.AppImage.sha256)\`
- Ed25519 signature: \`OpenBox-x86_64.AppImage.sig\` (verified against openbox-release.pub)

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v$(git tag --sort=-v:refname | head -1 | sed 's/^v//')...v$VERSION
NOTES

echo
echo "Pipeline complete. Review release-notes-$VERSION.md, then run:"
echo "  gh release create v$VERSION OpenBox-x86_64.AppImage OpenBox-x86_64.AppImage.zsync OpenBox-x86_64.AppImage.sha256 OpenBox-x86_64.AppImage.sig openbox-release.pub scripts/install.sh OpenBox-$VERSION-sbom.json --notes-file release-notes-$VERSION.md"
