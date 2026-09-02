# ADR 0022: ScreenScraper Provider

**Date:** 2026-09-02
**Status:** Accepted

## Context

OpenBox shipped two metadata providers (LaunchBox Games Database, IGDB) plus Steam/GOG/EmuMovies media. The emulation community's standard per-ROM-hash scraper — ScreenScraper — was absent, so ROM-based libraries could not be matched by hash, only by title similarity. ScreenScraper is community-run with per-user quotas, so integration must be cache-first and polite.

## Decision

Add `pkg/parity/parity_screenscraper.py` + `handlers/screenscraper.py` mirroring the IGDB provider pattern:

1. **Transport:** pure-stdlib `urllib` + `backend_io.read_limited`, thread-locked 1 request/second minimum, retry with backoff on 429/5xx only, JSON `output=json`.
2. **Credentials:** `~/.env` via `env_config` (`SCREENSCRAPER_USER`/`SCREENSCRAPER_PASSWORD`, optional dev pair). Never settings JSON; never exported.
3. **Disk cache:** `cache/screenscraper/<sha1 of params>.json` under the data dir, 30-day TTL, for `jeuInfos.php` and `userInfos.php` responses.
4. **Matching:** by `gameid`, or by ROM hash (`rommd5`/`romsha1`/`romcrc` + `romtype`, streamed with a 512 MB cap); platform→`systemeid` via a static table (absent platforms fall back to system-less hash matching, which the service supports).
5. **Media:** `medias[]` types map to OpenBox media kinds; per-kind winner chosen by region priority (settings.region_priority translated to SS region codes) then order; screenshots become a list. Downloads use the hardened `download_file` path and land under `media/screenscraper/<game-slug>/`.
6. **Routes (additive v2):** `GET status` (configured + cache size), `POST test` (userInfos), `GET search?q&platform` (jeuRecherche), `POST info` (one game's metadata for review), `POST apply` (durable job: metadata fields + optional media downloads; `replace_existing` defaults false), `POST match` (batch hash-match job over ≤100 games, cancellable).
7. **UI:** metadata dialog gains "Search ScreenScraper" and "ScreenScraper hash-match" actions; Settings → Integrations gains a credential-check card.

Deliberately NOT integrated into the LaunchBox match-review pipeline (`/api/v2/metadata/matches/*`): that pipeline is title-similarity based with class thresholds; hash matches are exact-or-miss and flow through their own reviewable apply job instead. Future unification can add SS as a match source without contract changes.

## Consequences

- ROM libraries get hash-exact metadata/media; nothing scrapes in the background — every network call is user-triggered.
- Quotas are respected via cache-first reads, throttling, and retry-only-on-429/5xx; the batch job caps at 100 games per run.
- The system-id table is data, correctable without touching logic.
- v1 surface untouched; new-module tests keep ≥85% coverage (achieved: 86% handler, 97% parity).
