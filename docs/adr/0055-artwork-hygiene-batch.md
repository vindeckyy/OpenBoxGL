# ADR 0055: Artwork hygiene batch policy and provider attribution

**Date:** 2026-09-16
**Status:** Accepted

## Context

SteamGridDB apply (`handlers/steamgrid.py`) could only fill empty covers one
game at a time. Users with large libraries had no way to find missing covers,
low-resolution art, wrong-aspect art, or duplicate images, and no safe bulk
fill. Any bulk write over existing artwork must be reversible and honest about
where the new image came from.

## Decision

- `pkg/parity/parity_artwork_hygiene.py` owns the read-only report:
  `build_report()` classifies `missing_cover`, `missing_file`, `low_res`
  (default shortest side < 200 px), `wrong_aspect` (per-kind target ratio with
  tolerance), and `duplicate` (same SHA-1 among primary covers). Image
  dimensions are parsed from PNG/JPEG/GIF/BMP/WebP headers with stdlib only.
- `select_fixable()` maps report issues to `(game_id, field)` replacement
  targets. Only fields that can be replaced are selected; `missing_file`
  entries are reported but never silently re-pointed.
- Replacements run as the cancelable `steamgrid-hygiene-fix` job. Before a
  file is overwritten the previous bytes are copied into
  `<data>/cache/artwork-hygiene/<batch_id>/` and a manifest is written
  atomically. `undo_batch()` restores replaced files and clears fields that
  were newly created; the state mutation for undo is the handler's job.
- Every written field records `artwork_provider = "SteamGridDB"` and the
  report exposes `provider_attribution` so the UI can credit the provider.
- Routes: `GET /api/v2/steamgrid/hygiene/report`,
  `POST /api/v2/steamgrid/hygiene/fix` (job), and
  `POST /api/v2/steamgrid/hygiene/undo`. v1 stays frozen.

## Consequences

- Fix-all is bounded (200 items per batch), cancelable, and undoable. Undo is
  byte-level for replaced files; a partial disk failure is reported per entry
  rather than failing the whole batch.
- The report walks file headers, so very large libraries pay proportional I/O
  only when the panel is opened.
- Duplicate detection is per primary cover; duplicates across other slots are
  reported as separate issues only when both files appear in the same slot.
