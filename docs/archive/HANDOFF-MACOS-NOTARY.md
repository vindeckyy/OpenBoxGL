# Handoff: macOS notarization spike (W0-06)

Date: 2026-10-29. Status: local hardened-runtime dry run executed with an ad-hoc identity. Notarization and stapling were not executed: no Developer ID certificate, no notary credentials, and no App Store Connect key exist in this environment.

## What ran

| Step | Result |
|---|---|
| Copied the 1.19.1 baseline bundle (`OpenBoxGL-release-1.19/build/macos/OpenBox.app`) to `/tmp/openbox-2.0/notary-spike` | Done |
| Re-signed each unique Mach-O file with `codesign --force --options runtime --timestamp=none --sign -` (12 unique files; the fat binaries are listed once per file) | 12 of 12 report the `runtime` flag |
| Re-signed the bundle with `--identifier io.openbox.GameLauncher` | `codesign --verify --deep --strict --verbose=2`: valid on disk; satisfies its Designated Requirement |
| Started `Contents/Resources/python/bin/python3.12 -B web_app.py --no-browser` with `OPENBOX_DATA_DIR` set to a temp directory, no entitlements | `GET /` 200, `GET /api/v1/library` 200, `GET /api/v1/settings` 200; the server accepted SIGTERM and exited after a few seconds |
| `import ssl, sqlite3, lzma, bz2, _ctypes, _sqlite3` in the hardened interpreter | Loaded |
| `ctypes.CDLL` on `Contents/Resources/python/lib/libpython3.12.dylib` | Refused with "different Team IDs" without `com.apple.security.cs.disable-library-validation`; loads with it |

## Findings

- The hardened runtime does not block the app's launch path. `otool -L` shows the interpreter does not link `libpython3.12.dylib` dynamically, so the refusal only affects code that `dlopen`s the embedded dylib.
- Ad-hoc signatures carry no team identifier, so library validation compares identifiers that do not match. A Developer ID build signs every component under one team identifier, which should make the entitlement unnecessary. That is an expectation until a Developer ID build runs.
- The `OpenBox` host executable (Cocoa and WebKit) was re-signed with the runtime flag but not launched. Launching it opens a window; its first hardened run belongs to the W5-09 manual matrix.
- The dry run used `--timestamp=none`. The release pipeline uses `--timestamp` against Apple's timestamp service, which cannot be exercised offline.
- `file` prints one line per architecture for universal binaries. Deduplicate paths before signing; a naive loop signs the fat binaries twice and then fails on the `(for architecture ...)` suffix.

## Go/no-go for W5-01

Conditional GO. The pipeline in ADR 0094 is implementable with the hardened runtime and an empty entitlement set. A notarized, stapled artifact is not verified here. It needs:

1. A Developer ID Application certificate and the `MACOS_*` secrets listed in ADR 0094.
2. A stable-tag release run that performs `notarytool submit --wait`, `stapler staple`, and `spctl --assess`.
3. The `spctl` output attached to the release.

Without the secrets the release ships ad-hoc with the "not notarized" caveat.

## Reproduce

```
cp -R build/macos/OpenBox.app /tmp/notary-spike/
# dedupe the Mach-O paths first; `file` prints one line per architecture
codesign --force --options runtime --timestamp=none --sign - <each unique Mach-O>
codesign --force --options runtime --timestamp=none --identifier io.openbox.GameLauncher --sign - OpenBox.app
codesign --verify --deep --strict --verbose=2 OpenBox.app
OPENBOX_DATA_DIR=$(mktemp -d) OpenBox.app/Contents/Resources/python/bin/python3.12 -B OpenBox.app/Contents/Resources/openbox/web_app.py --no-browser
```
