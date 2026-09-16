# ADR 0058: Signed emulator definition update channel trust model

**Date:** 2026-09-16
**Status:** Accepted

## Context

Emulator definitions are YAML files shipped in `emulator_defs/`. P1-24 made
the registry reload on file changes and report malformed definitions, but
there was no way to update definitions between OpenBox releases. Any remote
update channel executes configuration that builds launch commands, so it is
security-relevant.

## Decision

- The channel is opt-in (`emulator_defs_update_enabled`) and off by default.
  It requires an HTTPS manifest URL plus an HTTPS signature URL.
- Trust is anchored in the committed `openbox-release.pub` Ed25519 key, the
  same root of trust as AppImage updates. The manifest bytes are hashed
  (SHA-256) and the signature must verify over that digest before any file is
  written. A missing/short key, wrong algorithm, bad digest, or bad signature
  aborts the update with no writes.
- Manifest format v1: `{format: 1, version: "YYYY.MM.N", files: {name:
  sha256}, yaml: {name: body}}`. File names must match a safe
  `[A-Za-z0-9._-]+\.yaml` pattern (no traversal, no absolute paths), each
  body must match its SHA-256, and files/sizes are bounded (200 files,
  256 KiB each, 2 MiB manifest).
- Apply is per-file atomic (`atomic_write_text`), keeps the replaced file as
  `*.yaml.bak`, then calls `reload_defs()` so the registry and module maps
  refresh in the same request. Malformed definitions after apply are reported
  through the existing `definition_errors()` surface.
- Routes: `GET /api/v2/emulators/defs/channel` (status),
  `POST /api/v2/emulators/defs/channel/update` (fetch/verify/apply) and a
  Settings toggle + status line.

## Consequences

- Operating a channel requires signing the manifest with the release key;
  there is no unsigned path and no trust-on-first-use.
- A failed or tampered update leaves the installed definitions untouched.
- The bundled YAML defs remain the source of truth until an operator opts in.
