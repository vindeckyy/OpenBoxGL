# ADR 0039: LaunchBox resolve + remap + scale (amends 0033)

**Date:** 2026-09-04
**Status:** Accepted
**Amends:** ADR 0033 (LaunchBox library XML migration import)

## Context

ADR 0033 shipped a two-phase LaunchBox XML import (preview + apply) that
reported `EmulatorId` values but gave the user no way to resolve them, kept
Windows `C:\Games\...` paths verbatim (broken launchers on Linux), and parsed
the whole XML with `ET.parse` (DOM blowup on 20k-game exports, unbounded
50-row sample with no pagination).

## Decision

Additive `/api/v2` hardening only; v1 surface untouched.

1. **`POST /api/v2/import/launchbox/resolve`** accepts
   `{preview_id, mappings, path_remap}`. `mappings` translates LaunchBox
   `EmulatorId` → OpenBox `adapter_id` (or `emulator_id`) and is validated
   against the emulator-defs registry (`find_adapter` / `load_adapters`).
   Unknown targets are reported in `unresolved`, never silently applied.
   Recounts by re-streaming the source XML; the stored preview is never
   mutated.

2. **Windows path remap** (`path_remap: {from_prefix, to_dir}`):
   case-insensitive prefix match after backslash→slash normalization,
   e.g. `C:\Games\Quake\quake.exe` → `/mnt/games/Quake/quake.exe`.
   No remap (or no match) leaves the path for shelf-row handling.

3. **`needs_path` shelf rows, no shell synthesis**: after remap, any entry
   whose path is empty or still a Windows path becomes
   `{path: "", needs_path: true, launch: "", launchbox_path: <original>}`.
   Resolved rows gain `emulator_adapter_id` (+ registry `emulator_id`,
   preserving the LB id as `launchbox_emulator_id`). `launch` is always `""`
   (validated with `launch_tokens.find_invalid_tokens`); launch uses the
   tokenized `{path}` pipeline only.

4. **Stale preview, no new codes**: previews persist
   `{preview_id, revision: 1, xml_path, xml_fingerprint, library_signature}`
   under `launchbox_previews/`. Resolve rejects library or XML drift with
   reused `PREVIEW_STALE` (`PreviewStale`) and unknown ids with
   `PreviewNotFound`. No new `api_errors` codes.

5. **Streaming + 5k pagination (stdlib only)**: `iter_parsed_games` uses
   `ET.iterparse(end: Game)` + `elem.clear()`. `preview_import` streams
   counts/dedup and keeps only the `[offset:offset+limit]` page
   (`PREVIEW_PAGE_SIZE = 5000`, default limit 5000). 20k exports stream
   without a DOM; resolve truncates returned `rows` to 5k while counts
   reflect the full stream.

6. **Preview now persists**: `POST .../preview` uses
   `create_launchbox_preview` and returns `preview_id`/`revision` alongside
   the existing report keys (additive).

## Consequences

- Migration is completable: mappings resolve, Windows paths remap, leftovers
  are visible shelf rows instead of broken launchers or shell strings.
- Re-resolve is safe: no mutation, stale is explicit, unknown emulators stay
  reported.
- No UI locale change (no new user-visible strings); i18n stays 100%×5.
- v1 60-route contract unchanged; one additive v2 route.
