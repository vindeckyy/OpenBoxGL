# ADR 0013: Artifact-first release gates and support matrix

Date: 2026-08-25
Status: Accepted

## Context

OpenBox v1.7 is a flagship release for new Linux users. Shipping requires reproducible installable artifacts, explicit platform support, and gate checks that block publication until binaries pass—not documentation claims alone. The project adds no telemetry and does not conflate building a Flatpak bundle with Flathub store submission.

## Decision

### Artifact-first gates

- Release publication is gated on **exact-artifact CI** results, not source-only green checks.
- Required gates include backend tests, frontend lint/checkJs, performance targets (10k/20k library scenarios), coverage floors, shellcheck, desktop-file, and AppStream validation as defined in the v1.7 test plan.
- RC soak of **48–72 hours** on tagged prerelease artifacts precedes stable publication (F27).

### Platform matrix

| Dimension | v1.7 support |
|---|---|
| CPU architecture | **x86_64 only** |
| ARM64 | Deferred |
| Formal library scale | **20,000** games |

### AppImage

- Build and gate on **Ubuntu 22.04** runner targeting **x86_64** AppImage artifacts.
- AppImage is a release-gated deliverable, not an best-effort side build.

### Flatpak

- Target Flatpak runtime **25.08**.
- Produce and release-gate an installable Flatpak bundle aligned with the AppImage feature set.
- **No Flathub store submission** in v1.7; producing the bundle is separate from publishing to Flathub.

### Repository and release authority

- Git tags and GitHub Releases on the OpenBox repository are the canonical publication channel for v1.7 artifacts.
- Changelog and version sync checks must agree before stable release (F27); Unreleased entries must not claim shipped status early.

### Explicit exclusions

- **No telemetry** added in v1.7.
- No runtime dependency additions (Python standard library only in AppImage).
- No reduction of frozen v1 route surface.

## Consequences

- Users receive tested x86_64 AppImage and Flatpak bundles or the release does not ship.
- Support expectations are explicit: x86_64, 20k games, Ubuntu 22.04-built AppImage, Flatpak 25.08 runtime.
- Flathub submission remains a future, separate decision.
