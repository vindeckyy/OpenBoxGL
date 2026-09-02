# ADR 0024: ARM64 (aarch64) AppImage artifact matrix

**Date:** 2026-09-02
**Status:** Accepted
**Amends:** ADR 0013 (un-defers ARM64)

## Context

ADR 0013 deferred ARM64 for v1.7. The OpenBox AppImage bundles the host CPython interpreter and its shared libraries, so an artifact's architecture follows the build host architecture — there is no cross-compile step, only a per-arch build on a matching runner. ARM64 Linux handhelds and SBCs (Steam Deck alternatives, Raspberry Pi-class boards running desktop distros) are a growing share of the Linux gaming audience, and the runtime is already dependency-free stdlib Python plus a WebKitGTK native host that builds cleanly on aarch64.

The self-updater (`updates.py`) previously hard-coded `ASSET = "OpenBox-x86_64.AppImage"`, so an aarch64 user checking for updates would look for an x86_64 artifact and either find nothing or be offered the wrong architecture.

GitHub's `ubuntu-22.04-arm` runner image enters deprecation on 2026-09-17 and is fully unsupported by 2027-04-17, so a new aarch64 job must not target it.

## Decision

Ship a two-architecture AppImage release matrix: **x86_64** and **aarch64**.

1. **`build_appimage.sh`** is parameterized on `OPENBOX_ARCH` (default `uname -m`, normalized: `amd64`/`x64`→`x86_64`, `arm64`→`aarch64`). The output name, `appimagetool` download URL + pinned SHA-256, zsync name, and `ARCH=` env all follow the detected arch. The aarch64 `appimagetool` pin (`1b00524b…07fe`) was computed 2026-09-02 from the continuous release and is documented inline; the x86_64 pin is unchanged.
2. **`updates.py`** derives `ASSET` from the host arch via `_current_arch()`/`_arch_asset()`. `check_update` already keys the AppImage, checksum, and signature lookups off `ASSET` and raises when the asset is missing, so an aarch64 host is now offered only the aarch64 artifact and refuses a release that lacks it. No new refusal branch was needed.
3. **`scripts/release.sh`** builds the host arch (or `OPENBOX_ARCH`) and emits arch-suffixed artifacts: `OpenBox-<arch>.AppImage{,.zsync,.sha256,.sig}` and `OpenBox-<version>-<arch>-sbom.json`. `sign_release.py`, `verify_release.py`, and `gen_sbom.py` were already arch-agnostic (they operate on artifact paths passed as arguments) and need no changes.
4. **SBOM naming** moves from `OpenBox-<version>-sbom.json` to `OpenBox-<version>-<arch>-sbom.json` so the two artifacts in a release have distinct, unambiguous SBOMs. The existing `OpenBox-*-sbom.json` globs in tests and workflows still match.
5. **CI** (`release-appimage.yml`) builds and attests both arches in a matrix: x86_64 on `ubuntu-22.04` (unchanged), aarch64 on `ubuntu-24.04-arm` (the recommended, non-deprecating GitHub arm image). The publish job signs and uploads both. `ci.yml` matrices the `gate` job across the same two runners so the full coverage gate runs on aarch64 too.
6. **Native host** (`native_host.c`) builds unchanged on aarch64; the build scripts already fail loudly when `gcc`/`pkg-config`/`libwebkit2gtk-4.1-dev` are missing rather than shipping a stub.

## Consequences

- ARM64 users get a release-gated, signed AppImage and self-update path instead of a best-effort source run.
- A release now carries two AppImages, two zsyncs, two checksums, two signatures, and two SBOMs; the GitHub Release body lists all of them.
- The updater refuses an update when the running architecture's artifact is absent (e.g., an aarch64 host against an x86_64-only release) rather than installing the wrong arch.
- The x86_64 build runner (`ubuntu-22.04`) is itself on the 2026-09-17 deprecation path; migrating it to `ubuntu-24.04` is a separate follow-up (it would change the bundled glibc baseline and is out of 1.8.0 scope).
- Flathub submission remains a separate maintainer decision (ADR 0013); this ADR only governs the AppImage matrix.
