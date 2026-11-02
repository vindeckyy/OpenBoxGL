# OpenBox 2.1.0

**OpenBox 2.1 is the release where the 2.0 contracts become the only contracts: one public API, one native-host channel, one backup format to write, no library migration, and the compatibility layers 2.0 announced are gone.**

Your library, settings, plugins, and existing backups keep working. 2.1 does not change the state schema, backup format 2, plugin API 2, or sync format 3. A 2.0 build can open a 2.1 library, and a 2.1 build opens a 2.0 library without migrating it.

The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v2.0.0...v2.1.0](https://github.com/vindeckyy/OpenBoxGL/compare/v2.0.0...v2.1.0).

## What changes

- **The public API is `/api/v2` only.** `/api/v1` is removed. The `OPENBOX_ENABLE_V1_API` environment variable no longer restores it.
- **Unversioned `/api/*` paths are no longer dispatched.** They remain private implementation names used to build the `/api/v2` twins. Clients that call `/api/library` or `/api/jobs` get a `410` problem document naming the `/api/v2` replacement.
- **The native host channel is `/api/host`.** The legacy names `/api/launch`, `/api/shutdown`, and `/api/native/capabilities` are removed. Native windows shipped with 2.0 or later already use `/api/host` and keep working; a pre-2.0 host must be updated.
- **Backups write format 2 only.** Format 1 archives still restore, including archives written by 1.19 and the 2.0 compatibility export. New format 1 writes return `BACKUP_FORMAT_RETIRED`. If you need a backup that a 1.19 install can read, create it with a 2.0.x build before upgrading.
- **The jobs panel reads `/api/v2/jobs`.** The 1.19 job-name map and its `/api/jobs` route are gone.
- **Python 3.10 is deprecated.** It still works in 2.1 and remains in CI; 3.12 is recommended. Removal is planned for 3.0.
- **`openbox --rollback-library` remains available.** It still restores `library.v6.backup.json`; it is planned for removal in 3.0.

## What stays the same

| Contract | 2.0.0 | 2.1.0 |
|---|---|---|
| Public API | `/api/v2` | `/api/v2` (same 358 paths / 387 operations) |
| Native host channel | `/api/host` | `/api/host` (same 4 operations) |
| Library schema | 7 | 7 (no migration) |
| Backup format | Write 2, read 1 and 2 | Write 2, read 1 and 2 |
| Plugin API | 2, with the v1 adapter | 2, with the v1 adapter |
| Sync format | 3 | 3 |

A 2.0.0 build can read a 2.1.0 library because the schema and required fields did not change. A 2.1.0 build reads a 2.0.0 library without running a migration.

## Upgrade from 2.0

1. Quit OpenBox.
2. Install 2.1.0 over 2.0.0. The library is opened as format 7; no migration runs.
3. If a script calls `/api/v1`, an unversioned `/api/*` path, or a legacy host name, move it to `/api/v2` or `/api/host`.
4. If you may need a backup that a 1.19 install can read, create it on 2.0.x before upgrading. 2.1 can restore it, but cannot create it.

The detailed walkthrough is in [docs/upgrade-2.1.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/upgrade-2.1.md).

## Upgrade from 1.19

Upgrade to 2.0 first if you need the 1.19-compatible backup export. A direct 1.19 to 2.1 upgrade still migrates the library from schema 6 to 7, keeps `library.v6.backup.json`, and supports `openbox --rollback-library --yes`.

## Breaking changes

| Change | What it means | How to keep working |
|---|---|---|
| `/api/v1` removed | Scripts on v1 stop working; the env bridge is gone | Move to `/api/v2` |
| Unversioned `/api/*` removed | `/api/library`, `/api/jobs`, and other base paths return 410 | Use the `/api/v2` replacement in the error's `context.use` |
| Legacy host names removed | Pre-2.0 native windows are refused | Update the native host; the 2.0+ hosts already use `/api/host` |
| Backup format 1 write removed | 2.1 cannot create a 1.19-readable archive | Create it on 2.0.x; format 1 reads still restore |
| Python 3.10 deprecated | Still works; removal planned for 3.0 | Use Python 3.12 for source installs |

## Platforms

- **Linux x86_64 and aarch64 AppImages** keep the glibc 2.35 floor and the signed zsync update path.
- **Windows x86_64 and ARM64 zips** ship with their native host; the installer and updater select by CPU.
- **macOS** ships the universal bundle. The 2.1 bundle is ad-hoc signed; notarization remains the next platform step and is only claimed with `spctl` evidence.
- **Flatpak** builds against runtime 51 in `io.openbox.GameLauncher.yml`; `libwebkit2gtk-4.1.so.0` is present, and the bundle was built and installed in the 2.1 verification run.

## Get OpenBox 2.1.0

| Platform | Download | Notes |
|---|---|---|
| Linux x86_64 | `OpenBox-x86_64.AppImage` | glibc 2.35 floor; zsync available |
| Linux aarch64 | `OpenBox-aarch64.AppImage` | glibc 2.35 floor; zsync available |
| Linux Flatpak | `OpenBox-x86_64.flatpak` | x86_64 |
| Windows x86_64 | `OpenBox-x86_64-windows.zip` | Includes the native host |
| Windows ARM64 | `OpenBox-arm64-windows.zip` | Includes the ARM64 native host |
| macOS | `OpenBox-macos.zip` | Universal; ad-hoc signed |

The macOS installer pins the release tag:

```bash
VERSION=2.1.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

On Windows, extract the release archive and run `OpenBox\scripts\install.ps1`. On Linux, use the AppImage, the Flatpak, or `scripts/install.sh` as before.

## Also fixed in 2.1.0

- **Deep-link readiness uses a real route.** `openbox://` dispatch probes `GET /api/v2/version` instead of a GET health path that did not exist, so an already-running server is detected and a second server is not started.
- **The media checksum toggle works.** Settings > Library > Record media checksums now controls manifest writes, integrity hashing, and repair.
- **Covers, themes, sounds, video, reader documents, and RA badges use `/api/v2`.** The unversioned request path is no longer a second way into the UI's media.

## Known limits

- The macOS bundle is ad-hoc signed, so the first launch and each new build need the Privacy & Security confirmation.
- Linux-only features remain unavailable on Windows and macOS by design; they are hidden rather than broken.
- 1.19.x cannot read a format 7 library. Use `library.v6.backup.json` and `openbox --rollback-library` if you need to return to 1.19.
- 2.1 cannot create a 1.19-compatible backup. Keep a 2.0.x build if you still need one.
- Python 3.10 works but is deprecated; removal is planned for 3.0.
- The formal library scale remains 20,000 games. 50,000 remains exploratory until the 3.0 storage decision.
