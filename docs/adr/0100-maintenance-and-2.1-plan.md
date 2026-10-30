# ADR 0100: 2.0 maintenance and 2.1 plan

Status: Accepted (2026-10-29)

## Context

2.0 changes formats and removes the v1 surface by default. The maintenance policy has to say what gets fixed in patches, what waits for 2.1, and what is removed in 2.1.

## Decision

- **Patches (2.0.x)** fix defects only. They add no surface and change no format. A patch never migrates a library again.
- **2.0.1** is the planned window for any deferred artifact: the Windows ARM64 build if it did not ship in 2.0.0, and the Flathub submission if the maintainer authorizes it later.
- **2.1.0** removes the `/api/v1` bridge and the legacy unversioned host names, removes the v1 backup write path, retires the rollback command after the 2.1 release, and revisits the Python 3.10 floor with the AppImage base.
- **3.0.0** removes the Plugin API v1 adapter, reads state schema 6 only for export, and revisits the storage direction (ADR 0088, ADR 0098).
- Post-release, the release is watched for 14 days, and any release-blocking defect is fixed in 2.0.1. A defect that corrupts data gets a recovery guide and a build that restores from `library.v6.backup.json`.

## Consequences

- Deferred items are listed in the release notes with their target version, not silently dropped.

## Verification

`docs/RELEASE_NOTES.md` (known limits) and `docs/api-deprecations.md` (removal versions).
