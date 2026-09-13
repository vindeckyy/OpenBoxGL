# ADR 0046: Every Second Counts local-first surfaces

**Date:** 2026-09-12
**Status:** Accepted

## Context

The 1.11 release joins several user-facing surfaces that depend on the same
local-first guarantees: captures and reels, an Arcade Room, opt-in Household
records, and migration from Steam and ES-DE. These surfaces must remain useful
without a hosted account, must not expose arbitrary paths or credentials, and
must not weaken the frozen v1 route contract.

## Decision

- Record That stores only approved local media paths and uses the OBS replay
  buffer when it is configured and reachable. A bounded screenshot fallback is
  allowed; failed capture never fabricates a clip. Reels are deterministic
  manifests over approved clips and may render to MP4 only through the local
  ffmpeg tool.
- Arcade Room is a read-only canvas projection over the existing library and
  media APIs. Its texture cache is bounded, missing art has a generated
  fallback, and reduced-motion preferences select a static presentation. The
  optional Museum PIN uses a salted, versioned PBKDF2 digest and is documented
  as a convenience boundary, not authentication or a security boundary.
- Household records are opt-in, bounded, validated, and copied on merge. The
  leaderboard is computed locally from converged records; statistics are not
  emitted when sharing is disabled, and no server or account is introduced.
- Steam Bridge parses and writes the lossless binary VDF subset needed for
  `shortcuts.vdf`, preserving foreign records and rejecting stale plans before
  mutation. ES-DE imports follow the LaunchBox preview/apply model with source
  digests, explicit identities, bounded XML parsing, and transactional apply.
- These integrations are additive v2 routes. Their handlers are explicitly
  registered, have standalone contract tests, and do not alter the frozen v1
  route table.

## Consequences

The release can serve handheld migration, capture, kiosk browsing, and
household play without creating a new service dependency or cloud data model.
Some adapter capabilities remain deliberately conditional: OBS, ffmpeg, and
emulator savestate support are reported honestly when unavailable. Future
features must preserve approved-media checks, bounded payloads, stale-plan
rejection, and the local-only privacy defaults established here.
