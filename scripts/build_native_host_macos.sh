#!/bin/bash
# Build the macOS native host (WKWebView).
#
# Produces a universal binary (arm64 + x86_64) at the repository root, next
# to web_app.py, which is where openbox-native.sh looks for it. The host
# itself loads the app icon from the bundle it runs in, so the icon and the
# host ship together.
#
# Requires the Xcode Command Line Tools (clang and the macOS SDK). The
# WebKit and AppKit frameworks ship with macOS, so there is no SDK to
# download.
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$source_root"

output="${1:-$source_root/native_host}"
build_root="$source_root/build/macos"
mkdir -p "$build_root"

if ! command -v xcrun >/dev/null 2>&1; then
  echo "xcrun is required: install the Xcode Command Line Tools." >&2
  exit 1
fi

if ! xcrun --sdk macosx --show-sdk-path >/dev/null 2>&1; then
  echo "the macOS SDK is required: install the Xcode Command Line Tools." >&2
  exit 1
fi

if ! command -v codesign >/dev/null 2>&1; then
  echo "codesign is required: install the Xcode Command Line Tools." >&2
  exit 1
fi

arches=(arm64 x86_64)
slices=()
for arch in "${arches[@]}"; do
  slice="$build_root/native_host-$arch"
  echo "Building native_host for $arch..."
  xcrun --sdk macosx clang \
    -fobjc-arc -O2 -Wall -Werror \
    -mmacosx-version-min=11.0 \
    -arch "$arch" \
    native_host_mac.m \
    -framework Cocoa -framework WebKit \
    -o "$slice"
  slices+=("$slice")
done

echo "Merging the slices into a universal binary..."
lipo -create -output "$output" "${slices[@]}"
chmod 755 "$output"

# macOS refuses to launch some linker-signed binaries (the kernel kills them
# with SIGKILL), so give the host an explicit ad-hoc signature. The app bundle
# re-signs it with the bundle identifier when it is packaged.
codesign --force --sign - --timestamp=none --identifier io.openbox.native-host "$output" >/dev/null

archs="$(lipo -archs "$output")"
for arch in "${arches[@]}"; do
  case " $archs " in
    *" $arch "*) ;;
    *)
      echo "the merged binary is missing the $arch slice" >&2
      exit 1
      ;;
  esac
done

echo "Built $output ($archs)"
