# ADR 0056: ROM-hash matching without a LaunchBox schema change, and the auto-scrape job contract

**Date:** 2026-09-22
**Status:** Accepted

## Context

Flagship 2 ("effortless metadata") needs two things that pull in opposite
directions:

1. **ROM-hash matching.** The most reliable way to identify a ROM is by its
   content hash, not its filename. The natural place to look for hash data
   was the LaunchBox Games Database (LBDB) bulk download: if
   `Metadata.xml` carried ROM hashes, OpenBox could match locally with no
   network and no per-provider credentials.
2. **Automatic post-import scraping.** After an import, OpenBox should queue
   matching and media download as background jobs instead of making the
   user drive each step from the setup wizard.

## Decision

### No LBDB schema migration for ROM hashes

Verified 2026-09-22 against the local LBDB SQLite copy and against public
schema descriptions of the LaunchBox bulk `Metadata.xml`: the XML carries
**no ROM hash fields**. Some image records carry a `CRC32`, but that is
artwork-file integrity metadata, not a ROM hash. There is nothing to
import, so no SQLite schema change and no forced metadata-database
redownload is warranted — a migration would churn every install for zero
new data.

ROM-hash matching is implemented **through ScreenScraper only**
(`pkg/parity/parity_screenscraper.py`), which is hash-indexed by design:

- `confident_hash_match()` runs **two independent single-hash lookups**
  (MD5-only and CRC32-only) for one ROM and requires both to agree on the
  same ScreenScraper game id before reporting the `dual` tier. A single
  combined query cannot prove dual evidence, because the server matches on
  any one hash and the response does not reliably echo which hash matched.
- Tiers: `dual` (auto-apply eligible), `single` (one hash matched or the two
  disagreed), `title` (title fallback), `none`. Only `dual` is in
  `HASH_AUTO_APPLY_TIERS`; everything weaker is review-only and never
  auto-applied, including via the bulk `apply_confident` flag on
  `POST /api/v2/screenscraper/match`.
- The hash tier wins outright: a `dual` result never falls through to
  title. A hash mismatch or miss falls through to title search, which stays
  review-only.
- Applied hash matches record provenance on the game:
  `matched_by: "hash"`, `match_confidence: "dual"`, plus `screenscraper_id`.

### Auto-scrape: exactly two jobs per import batch

`queue_auto_scrape(import_batch_id)` in `handlers/metadata.py` submits
exactly:

- `metadata-match:{batch}` (`operation_type metadata.match_auto`) — creates
  and runs an LBDB match preview scoped to the batch
  (`create_match_preview_record` + `run_match_preview_job`), auto-applying
  only unique exact title/platform matches, then runs bounded online
  passes for still-unmatched games **only** when the corresponding opt-in
  setting is true *and* the provider is configured: ScreenScraper
  dual-hash matching for ROMs, IGDB exact-title matching.
- `metadata-media:{batch}` (`operation_type metadata.media_auto`) — waits
  for the match job to finish (bounded wait), then downloads LBDB media
  only for confidently matched IDs, plus a SteamGridDB fill for still-missing
  artwork kinds when opted in and configured.

Both jobs flow through the standard operation/SSE machinery, so they are
visible in the Activity Center like any other job.

**Default is offline.** `scrape_after_import` defaults to true; the three
provider opt-ins (`scrape_screenscraper_enabled`, `scrape_igdb_enabled`,
`scrape_steamgrid_enabled`) default to false. No online provider is invoked
unless its opt-in is true. Online passes are budgeted per run
(50 games each — ponytail: raise when a real library hits it), reuse the
providers' built-in throttling/retries, and never abort the job on a
single game's failure.

New route: `POST /api/v2/metadata/auto-scrape`. v1 is untouched.

## Consequences

- No metadata-database redownload is forced on users; the LBDB stays a
  title index, ScreenScraper stays the hash authority.
- Dual-hash auto-apply is conservative by construction: two independent
  corroborating lookups, or a human reviews it.
- The setup wizard's old frontend-driven match-preview + bulk-media
  sequence is replaced by one `auto-scrape` call when the toggle is on
  (default), so the wizard no longer duplicates job orchestration.
- Provider quotas are protected by opt-in defaults and per-run budgets;
  a failed provider pass degrades to "unmatched", never to a failed import.

## Implementation status (2026-09-22)

Shipped on `feat/effortless-metadata`: `queue_auto_scrape()` submits exactly
the two jobs above (`metadata.match_auto` / `metadata.media_auto` operation
types, Activity/SSE-visible); `POST /api/v2/metadata/auto-scrape` (202 on
queue, 200 with `queued: false` when the master toggle is off; empty
`media_types` is a valid match-only run); the setup wizard drives the single
call, renders the master toggle plus the three provider opt-ins (default
off), and persists all four through the owned
`GET`/`POST /api/v2/metadata/scrape-settings` — the settings handler's
normalization and the public-settings projection are untouched.
Exact-thumbnail apply via `media_urls` on the LaunchBox/SteamGridDB/
ScreenScraper apply routes (only URLs the provider/database returned for the
chosen record are downloaded), plus `GET /api/v2/metadata/media-candidates`,
backs the chooser dialog.
