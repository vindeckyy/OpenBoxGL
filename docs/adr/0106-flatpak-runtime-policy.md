# ADR 0106: Flatpak runtime support policy

Status: Accepted (2026-11-01)

## Context

The Flatpak manifest pins `org.gnome.Platform//51`, and the native host needs `webkit2gtk-4.1`. A runtime that is out of support, or that drops WebKitGTK 4.1, cannot be the Flathub base.

## Decision

- The Flatpak runtime must be a supported GNOME runtime and must ship `webkit2gtk-4.1`.
- Re-verify the runtime at every release; bump it in the manifest, the validator, the packaging test, and the release workflow together.
- If no supported runtime ships `webkit2gtk-4.1`, either pin the last runtime that does with an explicit risk note, or build WebKit in the manifest. A Flatpak artifact that cannot build is a channel problem, not a reason to delay the other artifacts.
- Flathub submission remains a separate maintainer decision (ADR 0096).

## Consequences

- The Flatpak channel may lag the other artifacts when the runtime moves.
- The manifest, validator, tests, and release workflow cannot disagree.

## Verification

Runtime 51 was installed on 2026-11-01 and `files/lib/x86_64-linux-gnu/libwebkit2gtk-4.1.so.0` is present. A full `flatpak-builder --user --state-dir=/tmp/...` build and bundle succeeded against runtime/SDK 51.

`scripts/validate_flatpak_manifest.py`, `tests/test_packaging.py`, `.github/workflows/release-flatpak.yml`, `docs/flathub-checklist.md`.
