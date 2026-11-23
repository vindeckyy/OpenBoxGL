#!/bin/bash
# Build the macOS app bundle: OpenBox.app with a universal native host, a
# universal Rust openbox-server binary, and the runtime assets (ADR 0115: zero
# bundled CPython).
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
bundle="$build_root/OpenBox.app"
work_root="$build_root/work"

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
command -v cargo >/dev/null 2>&1 || die "cargo is required to build openbox-server."

mkdir -p "$build_root"
rm -rf "$work_root"
mkdir -p "$work_root"

# --- 1. The universal Rust server binary ----------------------------------
say "== 1/8 Universal openbox-server =="
if [ -n "${OPENBOX_SERVER_BIN:-}" ] && [ -x "${OPENBOX_SERVER_BIN}" ]; then
  cp "$OPENBOX_SERVER_BIN" "$work_root/openbox-server"
else
  if command -v rustup >/dev/null 2>&1; then
    rustup target add aarch64-apple-darwin x86_64-apple-darwin
  fi
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
  cargo build --release --locked -p openbox-cli --target aarch64-apple-darwin \
    || die "aarch64-apple-darwin Rust build failed."
  cargo build --release --locked -p openbox-cli --target x86_64-apple-darwin \
    || die "x86_64-apple-darwin Rust build failed."
  lipo -create \
    "$source_root/target/aarch64-apple-darwin/release/openbox-server" \
    "$source_root/target/x86_64-apple-darwin/release/openbox-server" \
    -output "$work_root/openbox-server" \
    || die "lipo could not merge openbox-server."
fi
chmod 755 "$work_root/openbox-server"

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

cp "$work_root/openbox-server" "$bundle/Contents/MacOS/openbox-server"
chmod 755 "$bundle/Contents/MacOS/openbox-server"

share="$bundle/Contents/Resources/openbox"
mkdir -p "$share"
mkdir -p "$share/app"
cp "$source_root/app/plugin_runner_v2.py" "$share/app/"
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

# --- 4. The CLI shim ------------------------------------------------------
say "== 4/8 CLI shim =="
cat > "$bundle/Contents/Resources/bin/openbox" <<'SHIM'
#!/bin/bash
# The OpenBox command line. The bundle is four levels up from this script.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app="$(cd -- "$here/../../.." && pwd)"
share="$app/Contents/Resources/openbox"
server="$app/Contents/MacOS/openbox-server"
export OPENBOX_ROOT="$share"
if [ "${1:-}" = "--web" ]; then
  shift
  exec "$server" "$@"
fi
# Headless maintenance and Game Mode refusal bypass the native GUI host.
for arg in "$@"; do
  case "$arg" in
    --game-mode|--check-library|--repair-library|--migrate-library|--rollback-library|--dump-contracts)
      exec "$server" "$@" ;;
  esac
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
version="$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("app/updates.py").read(), re.M).group(1))')"
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
find "$bundle" -name "*.pyc" -delete 2>/dev/null || true
codesign "${sign_flags[@]}" ${sign_entitlements[@]+"${sign_entitlements[@]}"} --identifier "$BUNDLE_ID.server" "$bundle/Contents/MacOS/openbox-server" >/dev/null
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
python3 "$source_root/scripts/gen_sbom.py" --no-stdlib --version "$version" --appdir "$share" --out "$sbom" \
  || die "the SBOM could not be produced."

say "Built $output"
say "  bundle:  $bundle"
say "  sha256:  $output.sha256"
say "  sbom:    $sbom"
say "The bundle is ad-hoc signed: the first launch needs Privacy & Security > Open Anyway."
