# OpenBox 2.2.0

**OpenBox 2.2 ("The Long Game") protects your saves across sessions, plans your backlog across weeks, lets you write notes to your future self, turns your RetroAchievements cache into a hunt planner, and optionally watches Steam deals — without changing the v7 library format or adding a single runtime dependency.**

Your 2.0 and 2.1 libraries open without migration, and 2.1 still opens a 2.2 library cleanly (`docs/upgrade-2.2.md`). Everything works offline out of the box; only Deal Radar's opt-in `deals_enabled=true` mode talks to `store.steampowered.com`.

The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v2.1.0...v2.2.0](https://github.com/vindeckyy/OpenBoxGL/compare/v2.1.0...v2.2.0).

## What's new in 2.2

### 1. Save Vault (`save-vault/`)

- **Automatic or manual SHA-256 save snapshots.** Snapshot explicit `save_paths` and per-game discovered saves manually from the tabbed Save History dialog, automatically on game close (`save_vault_on_close`), or on a schedule (`save_vault_auto_days`). Automatic triggers exclude shared emulator roots (such as PCSX2 memory cards) unless explicitly added.
- **Mandatory pre-restore safety snapshot.** Restoring any snapshot first captures live saves into an unpruned `pre-restore` safety snapshot; if that capture fails, restore aborts before touching disk (`VAULT_SAFETY_SNAPSHOT_FAILED`).
- **Separate from `BACKUP_ITEMS`.** Snapshots live in `<data_dir>/save-vault/` so library ZIP backups stay bounded (`docs/backups.md`).

### 2. Achievement Trail (offline RetroAchievements analytics)

- Reads strictly from `cache/retroachievements/` and `cache/trail.json` with zero network calls during reads.
- `game_progress` now stores `id`, `date_earned`, and `date_earned_hardcore` additively (older caches label `"no date data — refresh to populate"` honestly until refreshed).
- Renders CSS-only 8-week unlock velocity charts, confidence-rated ETAs, 100-point milestones, and cheapest-first next-up queues in Insights and Game Details.

### 3. Deal Radar (opt-in Steam watchlist)

- **Off by default (`deals_enabled=false`).** Zero outbound connections until you review the host disclosure (`https://store.steampowered.com`, AppID & search queries only) and opt in.
- Redirects off `store.steampowered.com` are refused on every hop; manual entries track non-Steam deals with zero polling.
- Drop/target notifications use durable `last_notified_price_cents` tracking independent of notification feed eviction.

### 4. Player's Log — Time Capsules & Milestone Cards

- Seal notes or questions until a chosen local calendar date (`YYYY-MM-DD`); bodies are stripped server-side from every API payload until `unlock_at` passes and unlock once on startup/hourly sweeps or explicit open. ("Sealed" hides content in the app and is not on-disk encryption.)
- Derives 9 deterministic milestone kinds (game/library playtime, first clears, sessions, RA points/mastery, full-history play streaks, library age anniversaries, collection size) and exports offline 1200×630 PNG cards using the shared canvas renderer from `static/moments.js`.

### 5. Backlog Itinerary (multi-week planner)

- Builds a dated weekly schedule from unfinished backlog games using `quick-wins`, `comeback`, or `raided` ordering, ignoring `"Abandoned"`, trashed, and hidden games.
- Ranks estimates honestly (`IGDB` via bounded concurrency-2 job & 30-day `cache/igdb/` → `Own history` median from `>= 3` completed games → `Default` `8.0 h`) refined by RetroAchievements completion percentage, and derives live `done`/`on_track`/`behind`/`stale` statuses without cluttering settings.
