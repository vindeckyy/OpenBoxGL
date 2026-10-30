# ADR 0095: Windows ARM64 channel

Status: Accepted (2026-10-29). The ARM64 lane is in the release workflow and the installer selects it by architecture. The gates below hold on the record the repo can check: the WebView2 arm64 static loader is in the pinned SDK (docs/archive/HANDOFF-WINDOWS-ARM64.md) and the windows-11-arm CI job builds the host and runs the suite. The actual ARM64 build and run is external to this workstation; the first green tagged run is the remaining proof.

## Context

Windows ships x86_64 and ARM64. `scripts/install.ps1` selects the artifact by architecture and refuses a mismatch. A native ARM64 host links the WebView2 ARM64 static loader on a windows-11-arm runner.

## Decision

- The WebView2 ARM64 static library ships in the pinned SDK, the windows-11-arm CI job builds the host and runs the suite, and the release job builds, attests, signs, and publishes both Windows architectures.
- If the ARM64 lane ever fails on a release tag, the publish job's required artifacts fail the release rather than shipping half a channel.

## Consequences

- Until the ARM64 gate passes, ARM64 users get the refusal and the x86_64 emulation path.
- The Windows zip does not bundle Python; an ARM64 install needs an ARM64 CPython 3.10 or later.

## Verification

`docs/reliability.md` row 138 (Manual procedure). The architecture-selection code and its tests are not part of this release's changes.
