#!/bin/bash
# Build the macOS app bundle: OpenBox.app with a universal native host, a
# bundled CPython, and the runtime payload.
#
# The bundle is ad-hoc signed (no Developer ID, no notarization), so a
# downloaded copy needs one user action the first time: Privacy & Security
# > Open Anyway. The release signature (.sig plus .sha256) is what the
# installer and the in-app updater verify.
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$source_root"

output="${1:-$source_root/build/macos/OpenBox-macos.zip}"
build_root="$source_root/build/macos"
cache_root="$build_root/cache"
bundle="$build_root/OpenBox.app"
work_root="$build_root/work"

# --- Pinned inputs -------------------------------------------------------
# python-build-standalone release 20261021 (CPython 3.12.15). The hashes
# come from that release's SHA256SUMS file; a mismatch fails the build.
PBS_RELEASE="20261021"
PBS_PYTHON_VERSION="3.12.15"
PBS_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}"
PBS_ARM64_ASSET="cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PBS_ARM64_SHA256="a18cc33862b53bd54a148c710def5375c1b47470cccedf817a19c2d211764584"
PBS_X86_64_ASSET="cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-x86_64-apple-darwin-install_only.tar.gz"
PBS_X86_64_SHA256="869e0b30959beccc616ce14907439f4d0b3170c5830a2362dade07f07b0ed3b8"

# certifi carries the CA bundle the bundled interpreter trusts. It is a
# build-time input only: the runtime stays stdlib-only.
CERTIFI_VERSION="2026.10.23"
CERTIFI_WHEEL_URL="https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-${CERTIFI_VERSION}-py3-none-any.whl"
CERTIFI_WHEEL_SHA256="e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"

BUNDLE_ID="io.openbox.GameLauncher"

say() { printf '\033[1;32m%s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- Environment ----------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "the macOS app bundle can only be built on macOS."
command -v xcrun >/dev/null 2>&1 || die "xcrun is required: install the Xcode Command Line Tools."
command -v lipo >/dev/null 2>&1 || die "lipo is required: install the Xcode Command Line Tools."
command -v codesign >/dev/null 2>&1 || die "codesign is required: install the Xcode Command Line Tools."
command -v ditto >/dev/null 2>&1 || die "ditto is required."
command -v shasum >/dev/null 2>&1 || die "shasum is required."
command -v python3 >/dev/null 2>&1 || die "python3 is required."

mkdir -p "$cache_root" "$build_root"

# --- 1. The bundled interpreter ------------------------------------------
fetch() {
  local url="$1" target="$2"
  if [ ! -f "$target" ]; then
    say "Downloading $(basename "$target")..."
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --output "$target" "$url" \
      || die "download failed: $url"
  fi
}

verify_sha256() {
  local file="$1" expected="$2"
  local actual
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || die "checksum mismatch for $(basename "$file"): $actual != $expected"
}

say "== 1/8 Bundled interpreter =="
fetch "${PBS_BASE_URL}/${PBS_ARM64_ASSET}" "$cache_root/$PBS_ARM64_ASSET"
verify_sha256 "$cache_root/$PBS_ARM64_ASSET" "$PBS_ARM64_SHA256"
fetch "${PBS_BASE_URL}/${PBS_X86_64_ASSET}" "$cache_root/$PBS_X86_64_ASSET"
verify_sha256 "$cache_root/$PBS_X86_64_ASSET" "$PBS_X86_64_SHA256"
fetch "$CERTIFI_WHEEL_URL" "$cache_root/certifi-${CERTIFI_VERSION}-py3-none-any.whl"
verify_sha256 "$cache_root/certifi-${CERTIFI_VERSION}-py3-none-any.whl" "$CERTIFI_WHEEL_SHA256"

rm -rf "$work_root"
mkdir -p "$work_root/arm64" "$work_root/x86_64"
tar -xzf "$cache_root/$PBS_ARM64_ASSET" -C "$work_root/arm64"
tar -xzf "$cache_root/$PBS_X86_64_ASSET" -C "$work_root/x86_64"
arm_tree="$work_root/arm64/python"
x86_tree="$work_root/x86_64/python"
[ -d "$arm_tree" ] || die "the arm64 archive did not contain a python directory."
[ -d "$x86_tree" ] || die "the x86_64 archive did not contain a python directory."

# Merge the two trees into one universal interpreter: every Mach-O file is
# merged with lipo, every other file must be identical in both trees.
merged_tree="$work_root/python"
mkdir -p "$merged_tree"
while IFS= read -r relative; do
  if file -b "$arm_tree/$relative" | grep -q "Mach-O"; then
    mkdir -p "$(dirname "$merged_tree/$relative")"
    lipo -create "$arm_tree/$relative" "$x86_tree/$relative" -output "$merged_tree/$relative" \
      || die "lipo could not merge $relative"
  else
    # The interpreter and the pure-python standard library must be
    # identical in both builds. The headers, the build configuration, and
    # the bundled Tcl/Tk data carry the build paths, so they differ and only
    # the arm64 copy is kept.
    case "$relative" in
      lib/python3.12/_sysconfigdata*) ;;
      bin/*|lib/python3.12/*.py)
        cmp -s "$arm_tree/$relative" "$x86_tree/$relative" \
          || die "the two interpreter builds differ in $relative"
        ;;
    esac
    mkdir -p "$(dirname "$merged_tree/$relative")"
    cp "$arm_tree/$relative" "$merged_tree/$relative"
  fi
done <<EOF
$(cd "$arm_tree" && find . -type f | sed 's|^\./||' | sort)
EOF
while IFS= read -r relative; do
  mkdir -p "$(dirname "$merged_tree/$relative")"
  ln -sf "$(cd "$(dirname "$x86_tree/$relative")" && readlink "$(basename "$relative")")" "$merged_tree/$relative"
done <<EOF
$(cd "$arm_tree" && find . -type l | sed 's|^\./||' | sort)
EOF
while IFS= read -r relative; do
  merged_archs="$(lipo -archs "$merged_tree/$relative" 2>/dev/null || true)"
  for arch in arm64 x86_64; do
    case " $merged_archs " in
      *" $arch "*) ;;
      *) die "the merged file $relative is missing the $arch slice ($merged_archs)" ;;
    esac
  done
done <<EOF
$(cd "$merged_tree" && find . -type f | sed 's|^\./||' | while read -r f; do
  if file -b "$merged_tree/$f" | grep -q "Mach-O"; then echo "$f"; fi
done)
EOF

# --- 2. The native host ---------------------------------------------------
say "== 2/8 Native host =="
bash "$source_root/scripts/build_native_host_macos.sh" "$work_root/native_host" >/dev/null \
  || die "the native host did not build."

# --- 3. The bundle --------------------------------------------------------
say "== 3/8 Bundle layout =="
rm -rf "$bundle"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources/bin"
printf 'APPL????' > "$bundle/Contents/PkgInfo"

cp "$work_root/native_host" "$bundle/Contents/MacOS/OpenBox"
chmod 755 "$bundle/Contents/MacOS/OpenBox"

cp -R "$merged_tree" "$bundle/Contents/Resources/python"

share="$bundle/Contents/Resources/openbox"
mkdir -p "$share"
while IFS= read -r module; do
  [ -n "$module" ] || continue
  case "$module" in
    *.py) ;;
    *) continue ;;
  esac
  mkdir -p "$share/$(dirname "$module")"
  cp "$source_root/$module" "$share/$module"
done < "$source_root/runtime_modules.txt"
cp "$source_root/index.html" "$source_root/openbox.svg" "$share/"
cp "$source_root/openbox-release.pub" "$share/"
mkdir -p "$share/static"
cp "$source_root"/static/*.js "$source_root"/static/*.css "$share/static/"
for svg in "$source_root"/static/*.svg; do
  [ -e "$svg" ] && cp "$svg" "$share/static/"
done
mkdir -p "$share/locales"
cp "$source_root"/locales/*.json "$share/locales/"
mkdir -p "$share/assets/mame" "$share/assets/shaders"
cp "$source_root/assets/openbox-logo.png" "$share/assets/"
cp "$source_root/assets/placeholder-cover.svg" "$share/assets/"
cp "$source_root/assets/mame/scanlines.png" "$share/assets/mame/"
cp "$source_root/assets/shaders/scanlines.slangp" "$share/assets/shaders/"
mkdir -p "$share/emulator_defs"
cp "$source_root"/emulator_defs/*.yaml "$share/emulator_defs/"
mkdir -p "$share/plugins"
cp "$source_root/plugins/catalog.json" "$share/plugins/"
mkdir -p "$share/themes"
cp "$source_root"/themes/*.css "$share/themes/"
cp "$source_root/LICENSE" "$share/"

# The CA bundle the bundled interpreter trusts.
python3 - "$cache_root/certifi-${CERTIFI_VERSION}-py3-none-any.whl" "$share/cacert.pem" <<'PY'
import sys, zipfile
wheel, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(wheel) as package:
    data = package.read("certifi/cacert.pem")
with open(target, "wb") as handle:
    handle.write(data)
PY

# --- 4. The CLI shim ------------------------------------------------------
say "== 4/8 CLI shim =="
cat > "$bundle/Contents/Resources/bin/openbox" <<'SHIM'
#!/bin/bash
# The OpenBox command line. The bundle is four levels up from this script.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app="$(cd -- "$here/../../.." && pwd)"
share="$app/Contents/Resources/openbox"
if [ "${1:-}" = "--web" ]; then
  shift
  exec "$app/Contents/Resources/python/bin/python3" -B "$share/web_app.py" "$@"
fi
# Game Mode needs gamescope, so web_app.py refuses it on macOS (exit 2) rather than opening a window.
for arg in "$@"; do
  if [ "$arg" = "--game-mode" ]; then
    exec "$app/Contents/Resources/python/bin/python3" -B "$share/web_app.py" "$@"
  fi
done
exec "$app/Contents/MacOS/OpenBox" "$@"
SHIM
chmod 755 "$bundle/Contents/Resources/bin/openbox"

# --- 5. The icon ----------------------------------------------------------
say "== 5/8 Icon =="
iconset="$work_root/OpenBox.iconset"
rm -rf "$iconset"
mkdir -p "$iconset"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$source_root/assets/openbox-icon-1024.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$source_root/assets/openbox-icon-1024.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$bundle/Contents/Resources/OpenBox.icns"

# --- 6. Info.plist --------------------------------------------------------
say "== 6/8 Info.plist =="
version="$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("updates.py").read(), re.M).group(1))')"
python3 - "$bundle/Contents/Info.plist" "$version" "$BUNDLE_ID" <<'PY'
import plistlib
import sys

path, version, identifier = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {
    "CFBundleIdentifier": identifier,
    "CFBundleExecutable": "OpenBox",
    "CFBundleName": "OpenBox",
    "CFBundleDisplayName": "OpenBox",
    "CFBundlePackageType": "APPL",
    "CFBundleIconFile": "OpenBox",
    "CFBundleShortVersionString": version,
    "CFBundleVersion": version,
    "CFBundleInfoDictionaryVersion": "6.0",
    "LSMinimumSystemVersion": "11.0",
    "NSHighResolutionCapable": True,
    "LSApplicationCategoryType": "public.app-category.games",
    "CFBundleURLTypes": [
        {
            "CFBundleURLName": identifier,
            "CFBundleURLSchemes": ["openbox"],
        }
    ],
    # The web app is served over loopback HTTP.
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    "NSDesktopFolderUsageDescription": "OpenBox reads your game folders so it can import and launch your library.",
    "NSDocumentsFolderUsageDescription": "OpenBox reads your game folders so it can import and launch your library.",
    "NSDownloadsFolderUsageDescription": "OpenBox reads your game folders so it can import and launch your library.",
    "NSRemovableVolumesUsageDescription": "OpenBox reads your game folders so it can import and launch your library.",
    "NSNetworkVolumesUsageDescription": "OpenBox reads your game folders so it can import and launch your library.",
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle)
PY

# --- 7. Signing -----------------------------------------------------------
# Ad-hoc by default: that is the shipped behavior until a release runs with secrets.
# With OPENBOX_DEVELOPER_ID set, every Mach-O file and the bundle are signed with that
# Developer ID identity, the hardened runtime, a secure timestamp, and the entitlements in
# scripts/macos/OpenBox.entitlements (ADR 0094). Notarization runs only when
# OPENBOX_NOTARY_PROFILE names a notarytool keychain profile (below).
identity="${OPENBOX_DEVELOPER_ID:-}"
if [ -n "$identity" ]; then
  say "== 7/8 Developer ID signature (hardened runtime) =="
  sign_flags=(--force --options runtime --timestamp --sign "$identity")
  sign_entitlements=(--entitlements "$source_root/scripts/macos/OpenBox.entitlements")
else
  say "== 7/8 Ad-hoc signature =="
  sign_flags=(--force --sign - --timestamp=none)
  sign_entitlements=()
fi
find "$bundle/Contents/Resources/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$bundle" -name "*.pyc" -delete 2>/dev/null || true
while IFS= read -r file; do
  codesign "${sign_flags[@]}" ${sign_entitlements[@]+"${sign_entitlements[@]}"} "$file" >/dev/null 2>&1 \
    || die "codesign failed for $file"
done <<EOF
$(find "$bundle/Contents/Resources/python" -type f | while read -r f; do
  if file -b "$f" | grep -q "Mach-O"; then echo "$f"; fi
done)
EOF
codesign "${sign_flags[@]}" ${sign_entitlements[@]+"${sign_entitlements[@]}"} --identifier "$BUNDLE_ID" "$bundle/Contents/MacOS/OpenBox" >/dev/null
codesign "${sign_flags[@]}" ${sign_entitlements[@]+"${sign_entitlements[@]}"} --identifier "$BUNDLE_ID" "$bundle" >/dev/null
codesign --verify --strict --verbose=2 "$bundle" >/dev/null 2>&1 || die "the bundle signature does not verify."

# --- 8. Smoke test and packaging -----------------------------------------
say "== 8/8 Smoke test =="
python3 -B "$source_root/scripts/macos_bundle_smoke.py" "$bundle" || die "the bundle smoke test failed."

# Notarization contacts Apple and needs a keychain profile made with `xcrun notarytool
# store-credentials`. The stapled ticket is attached to the bundle before the archive is
# built, so the shipped archive carries it.
if [ -n "$identity" ] && [ -n "${OPENBOX_NOTARY_PROFILE:-}" ]; then
  say "== Notarizing with notarytool (profile $OPENBOX_NOTARY_PROFILE) =="
  mkdir -p "$work_root"
  notary_archive="$work_root/notarize-$(basename "$output")"
  rm -f "$notary_archive"
  ditto -c -k --sequesterRsrc --keepParent "$bundle" "$notary_archive"
  xcrun notarytool submit "$notary_archive" --keychain-profile "$OPENBOX_NOTARY_PROFILE" --wait \
    || die "notarization was not accepted; see the notarytool log."
  xcrun stapler staple "$bundle" || die "stapling the notarization ticket failed."
  xcrun stapler validate "$bundle" || die "stapler validation failed."
  spctl --assess --type execute --verbose=2 "$bundle" || die "Gatekeeper rejects the notarized bundle."
  say "Notarized and stapled."
fi
mkdir -p "$(dirname "$output")"
rm -f "$output"
ditto -c -k --sequesterRsrc --keepParent "$bundle" "$output"
# The sidecar must name the archive by basename, because the installer and
# the release smoke job verify it from the directory they downloaded into.
( cd "$(dirname "$output")" && shasum -a 256 "$(basename "$output")" > "$(basename "$output").sha256" )

sbom="$(dirname "$output")/OpenBox-${version}-macos-sbom.json"
python3 "$source_root/scripts/gen_sbom.py" --version "$version" --appdir "$share" --out "$sbom" \
  || die "the SBOM could not be produced."

say "Built $output"
say "  bundle:  $bundle"
say "  sha256:  $output.sha256"
say "  sbom:    $sbom"
say "The bundle is ad-hoc signed: the first launch needs Privacy & Security > Open Anyway."
