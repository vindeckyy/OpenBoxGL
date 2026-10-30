# ADR 0094: macOS notarization

Status: Not adopted for 2.0 (2026-10-29). The maintainer decided the 2.0 macOS bundle ships ad-hoc signed, so no notarized artifact is produced and no Developer ID secrets are needed. The machinery below stays in the tree for a later release; the hardened-runtime dry run passed locally with an ad-hoc identity (`docs/archive/HANDOFF-MACOS-NOTARY.md`).

## Context

The macOS bundle is ad-hoc signed. Every first launch asks the user to allow it in Privacy & Security, and permissions can need re-granting after an update.

## Decision

- The 2.0 macOS bundle ships ad-hoc signed. The install caveat ("the bundle is not notarized") stays, and the release notes say so plainly.
- The notarization machinery stays in the tree for a later release: Developer ID signing with the hardened runtime, `notarytool submit --wait`, `xcrun stapler staple`, then `spctl --assess` and `stapler validate`.
- Entitlements are the narrowest set that makes the bundle run under the hardened runtime. Each entitlement is recorded here with its reason. The file is `scripts/macos/OpenBox.entitlements`.
- Without the secrets, the release job builds ad-hoc and the release notes say the bundle is not notarized. A stable tag never claims notarization without `spctl` evidence.
- `NOTARIZED=1` in the release environment removes the "not notarized" caveat from the install docs; nothing else changes the caveat.

## Entitlement record

| Entitlement | In the file | Reason |
|---|---|---|
| none | No keys; the file holds an empty dictionary | The dry run re-signed every unique Mach-O file with `--options runtime`. The bundle reports `runtime`, `codesign --verify --deep --strict` passes, and the server served `/`, `/api/v1/library`, and `/api/v1/settings` with HTTP 200 under the hardened runtime. |
| `com.apple.security.cs.disable-library-validation` | No | Needed only for ad-hoc signing, and only for `dlopen` of the bundled `libpython3.12.dylib`: without it the load fails with "different Team IDs". The app's launch path does not load that dylib (`python3.12` does not link it). A Developer ID build signs every component under one team identifier, so the entitlement is expected to be unnecessary there. That expectation is unverified until a Developer ID build runs. |

Absent by design: `allow-jit`, `allow-unsigned-executable-memory`, `allow-dyld-environment-variables`, and `get-task-allow`. CPython 3.12 generates no code at runtime, and no repository file reads `DYLD_*` variables (checked by grep on 2026-10-29).

## Consequences

- The "not notarized" caveat stays until a release run with secrets produces `spctl` evidence.
- Notarization sends the bundle to Apple. The app itself still has no runtime account.
- A new entitlement is added only to the table above, with its reason, and only after a hardened-runtime failure is reproduced.

## Verification

- Dry run: `docs/archive/HANDOFF-MACOS-NOTARY.md` (W0-06, ad-hoc identity, 2026-10-29).
- `docs/reliability.md` row 137 (manual procedure, Developer ID build).
- `scripts/check_macos_release.py` (W5-01) verifies the entitlements file and the signing flags in the build script. It is a static gate; it does not replace `spctl`.
