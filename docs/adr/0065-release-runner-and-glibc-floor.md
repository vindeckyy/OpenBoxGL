# ADR 0065: Release runner image and the glibc floor

**Date:** 2026-10-08
**Status:** Proposed (1.16.1). The container build is verified locally (see Evidence); the change to `release.yml` waits for a CI dry run on a tag, because GitHub's runner cannot be exercised here.

## Context

The release workflow builds the x86_64 AppImage, the aarch64 AppImage and the Windows
native host on `ubuntu-22.04` (`.github/workflows/release.yml`). GitHub is retiring the
Ubuntu 22 runner images. The runner-images announcements we checked put the start of
deprecation at 2026-09-17 and full removal at 2027-04-17; the exact dates were not
confirmed against the upstream changelog.

An AppImage carries the glibc of its build host. Moving the build to `ubuntu-24.04`
raises that floor from 2.35 to 2.39, which drops users on Ubuntu 22.04 and Debian 12.
That is a silent compatibility regression, so the floor is part of the decision, not
a side effect of the runner.

## Decision

Keep the glibc floor at 2.35 by running the AppImage build inside a `ubuntu:22.04`
container, on an `ubuntu-24.04` runner that is still supported. The container's
userland is what the AppImage is linked against; the host only supplies Docker.

The native host (`native_host`, built with the WebKit 4.1 headers) follows the same
rule where it is built on Linux, so its floor does not move either.

Adoption requires, before a tag is cut:

1. A dry run of the AppImage job on a branch, using the containerised build.
2. `ldd --version` inside the container reports 2.35, and the produced AppImage's
   highest required `GLIBC_` symbol is no newer than 2.35 (`objdump -T` over the
   bundled binaries).
3. The `flatpak-validate` and AppImage jobs pass on the same commit.

## Consequences

- The release keeps the Ubuntu 22.04 compatibility promise without depending on a
  retiring runner image.
- Maintenance cost: one container image pin to refresh when 22.04 support ends.
- Until the dry run passes, the release stays on `ubuntu-22.04`. The retirement date
  is the deadline to finish the dry run, not a reason to switch unverified.

## Evidence (1.16.1, 2026-10-08)

Run locally with Podman, on a scratch copy of the tree, not on the release runner:

- `docker.io/library/ubuntu:22.04` reports `ldd (Ubuntu GLIBC 2.35-0ubuntu3.15) 2.35`.
- `build_appimage.sh` completed inside that container and produced a 10 MB
  `OpenBox-x86_64.AppImage`, with `APPIMAGE_EXTRACT_AND_RUN=1` so no FUSE is needed.
- Extracting that AppImage and scanning all 69 ELF files for their `GLIBC_` symbol versions:
  the highest version required is `GLIBC_2.35`. The floor holds.

Not verified here: the same build on an `ubuntu-24.04` host (expected floor 2.39), the aarch64
build, and the GitHub container job itself. The release comment says aarch64 already builds on
`ubuntu-24.04-arm`, so today that artefact's floor is probably the 24.04 glibc. This ADR's
change would fix that as well; it has not been measured.

## What the CI dry run must show

1. `release.yml` builds the x86_64 and aarch64 AppImages in `ubuntu:22.04` containers on a
   24.04 runner. The steps that call `sudo` need a root-safe form inside the container.
2. The AppImage job's `ldd`-style check above passes for both arches; the aarch64 number is
   measured, not assumed.
3. `flatpak-validate` and the AppImage smoke pass on the same commit.
