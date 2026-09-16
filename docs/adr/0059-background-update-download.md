# ADR 0059: Background update download with apply-on-restart

**Date:** 2026-09-16
**Status:** Accepted

## Context

`updates.install_update` downloads the verified AppImage synchronously in the
request thread and replaces the file immediately. A 200 MB download blocks the
UI and the user cannot leave the update dialog. The 1.13 milestone asks for a
background download that applies on restart, with honest progress, without
weakening the existing checksum + signature verification.

## Decision

- `updates.background_download_update()` streams the trusted release asset to
  a hidden temporary file next to the AppImage, verifies the SHA-256 checksum
  and Ed25519 signature first, and only then atomically replaces the AppImage,
  keeping the previous build as `<name>.previous.AppImage`. Replacing the file
  on disk *is* apply-on-restart: the running process keeps its inode, and the
  next start executes the new build.
- The download runs as the cancelable `update-download` job. Progress reports
  `(downloaded, total)` bytes; when the server does not send `Content-Length`
  the UI shows downloaded bytes instead of a fake percentage. Cancellation is
  polled between 1 MiB chunks and removes the temporary file.
- `POST /api/v2/update/download` starts the job; `GET /api/v2/update/download/status`
  reports current version, AppImage availability, and the
  `update_auto_download` toggle. Both are opt-in and require `APPIMAGE`.
- No new transport or signature scheme is introduced; this reuses the release
  channel, the committed key, and the existing `.previous` backup convention.

## Consequences

- The user can keep playing while the update downloads; a cancelled or failed
  download never touches the current AppImage.
- The staged build is applied on the next start even if OpenBox is running for
  days, which matches the honest "restart to use it" message.
- Auto-download is a setting; nothing downloads without the operator enabling
  it or pressing "Download update now".
