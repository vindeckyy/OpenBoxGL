# Upgrading to OpenBox 2.2 ("The Long Game")

## Compatibility & Zero Migration

- **State schema stays v7.** `library.json` keeps `schema_version: 7`. Opening a 2.0 or 2.1 library in 2.2 requires zero migration; opening a 2.2 library in 2.1 or 2.0 also succeeds cleanly.
- **Additive top-level fields:** 2.2 introduces three optional top-level keys (`"deals"`, `"time_capsules"`, `"itinerary"`) normalized with warn-and-repair in `state_store.py`.
- **New `KNOWN_SETTINGS` keys (8):** `save_vault_enabled`, `save_vault_keep`, `save_vault_on_close`, `save_vault_auto_days`, `deals_enabled` (false by default), `deals_poll_hours`, `deals_min_drop_pct`, `deals_store_cc`. Unknown-key pruning in 2.1 may drop those keys if saved under 2.1; your games, saves, and sessions stay intact.

## New Data Directories & Backups

- **Save Vault (`save-vault/`):** Save-file snapshots live outside `library.json` and outside standard `BACKUP_ITEMS` archives so weekly library backups do not bloat uncontrollably. Back up `save-vault/` manually or exclude/copy it with your disk backup strategy (`docs/backups.md`).
- **Derived caches (`cache/`):** `cache/trail.json`, `cache/deals/`, and `cache/igdb/` are pure derived/response caches (`0o600`) that rebuild safely if deleted.
- **RetroAchievements unlock dates:** Older caches created in 2.1 lack `date_earned` / `date_earned_hardcore` fields; Achievement Trail reports `"no date data — refresh to populate"` honestly until you run **Refresh RA progress**.

## Privacy & Security Expectations

- **Deal Radar is off by default (`deals_enabled=false`).** Zero sockets open while off. When enabled, only watched Steam AppIDs and search queries are sent to `https://store.steampowered.com` (no cookies, no account), and HTTP redirects off `store.steampowered.com` are refused.
- **Time Capsules ("sealed ≠ encrypted"):** Sealed capsules are redacted server-side (`body` is omitted from API payloads before `opened_at` is set), **not** encrypted on disk. Capsule text sits alongside normal library JSON in `library.json`.
