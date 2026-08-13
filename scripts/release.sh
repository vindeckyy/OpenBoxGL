#!/usr/bin/env bash
# OpenBox release pipeline: everything mechanical up to the human approval.
#
#   ./scripts/release.sh
#
# Runs: version sync -> make check gate -> AppImage build -> SBOM -> signing
# (if a key is available) -> release notes draft. The final `gh release`
# publish is intentionally left to the maintainer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() { echo "release.sh: $*" >&2; exit 1; }

echo "== 1/6 version sync =="
python3 scripts/check_version_sync.py

echo "== 2/6 verification gate =="
make check

echo "== 3/6 AppImage build =="
bash build_appimage.sh "$PWD/OpenBox-x86_64.AppImage"
[ -f OpenBox-x86_64.AppImage ] || fail "AppImage missing after build"

VERSION="$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("updates.py").read(), re.M).group(1))')"

echo "== 4/6 SBOM =="
python3 scripts/gen_sbom.py --version "$VERSION" --out "OpenBox-$VERSION-sbom.json"

echo "== 5/6 checksum + signature =="
sha256sum OpenBox-x86_64.AppImage | tee OpenBox-x86_64.AppImage.sha256
if [ -n "${OPENBOX_SIGNING_KEY:-}" ] && [ -f "$OPENBOX_SIGNING_KEY" ]; then
  python3 scripts/sign_release.py --key "$OPENBOX_SIGNING_KEY" --out "OpenBox-x86_64.AppImage.sig" OpenBox-x86_64.AppImage
  python3 scripts/verify_release.py --key openbox-release.pub OpenBox-x86_64.AppImage OpenBox-x86_64.AppImage.sig
else
  echo "no OPENBOX_SIGNING_KEY set; skipping signature (release will be unsigned)"
fi

echo "== 6/6 release notes draft =="
cat > "release-notes-$VERSION.md" <<NOTES
# OpenBox v$VERSION

$(sed -n '/^## Unreleased/,/^## \[/p' CHANGELOG.md | sed '1d;$d')

## Verification

- \`make check\`: lint, compile, $({ ls test_*.py | wc -l; }) test files, coverage floors green.
- SBOM: \`OpenBox-$VERSION-sbom.json\` (CycloneDX 1.4)
- SHA-256: \`$(cut -d' ' -f1 OpenBox-x86_64.AppImage.sha256)\`

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v$(git tag --sort=-v:refname | head -1 | sed 's/^v//')...v$VERSION
NOTES

echo
echo "Pipeline complete. Review release-notes-$VERSION.md, then run:"
echo "  gh release create v$VERSION OpenBox-x86_64.AppImage OpenBox-x86_64.AppImage.zsync OpenBox-x86_64.AppImage.sha256 OpenBox-$VERSION-sbom.json --notes-file release-notes-$VERSION.md"
