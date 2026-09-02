# OpenBox v1.8.0: Navigation, Scraping & ARM64

**OpenBox v1.8.0** introduces keyboard and gamepad library navigation, hash routing, a ScreenScraper per-ROM-hash scraping provider, custom gamescope presets with per-game override, library export to JSON/CSV, and aarch64 AppImage release artifacts alongside x86_64.

---

### � Keyboard & Gamepad Navigation
* **Grid and List Navigation**: Arrows/Home/End/Page-Up/Down move the focus with virtualization-aware reveal; `f` favorites, Escape clears, and gamepad input runs through a configurable controller map with edge detection.
* **Hash Routing**: Refresh and shared links restore platform/playlist/preset/query/selection/sort via `#/key/value` hash fragments (ADR 0021).
* **Sortable List Columns**: Click list-view headers to cycle sort direction; the choice persists via `list_sort`/`list_sort_dir` settings.
* **Lightbox & Skeletons**: Screenshot lightbox with prev/next/zoom and a counter; cover skeleton shimmer removed on load.

---

### � ScreenScraper Provider
* **Per-ROM-Hash Scraping**: `pkg/parity/parity_screenscraper.py` hashes ROMs (md5/sha1/crc, 512 MB cap) and matches them against ScreenScraper's jeuRecherche/jeuInfos endpoints (ADR 0022).
* **Throttled & Cached**: 1 req/s thread-locked throttle, 429/5xx backoff, and a 30-day disk cache under `cache/screenscraper/`.
* **Region-Priority Media**: `choose_media()` picks media by `settings.region_priority`; `clean_media_url()` enforces https-only URLs.
* **Additive v2 Routes**: status/test/search/info/match (batch ≤100, cancellable)/apply (durable job that downloads media outside the state lock).
* **UI**: Search and hash-match actions in the metadata dialog; credential check card in Settings → Integrations. Credentials live in `~/.env` (`SCREENSCRAPER_USER`/`PASSWORD`).

---

### � Custom Gamescope Presets
* **User-Defined Presets**: Up to 16 custom presets with unique names and bounded integer args; custom names shadow stock presets.
* **Per-Game Override**: A per-game `gamescope_preset` field wins over the global preset at launch (completes ADR 0016).
* **Editor UI**: Settings → Controller editor plus a per-game select in the game editor Launch tab.

---

### 📤 Library Export
* **JSON & CSV**: `POST /api/v2/library/export` queues a durable job (Activity Center, cancellable) with `all`/`platform`/`playlist` scopes (ADR 0023).
* **Shareable by Construction**: Only the game-field projection is exported; settings, credentials, webhooks, and history are never included. Media paths are opt-in.
* **Rotation**: Files land in `<data dir>/exports/`, newest 10 kept; download validated by name regex + directory containment.

---

### 🖥️ ARM64 & Flathub Prep
* **aarch64 AppImage**: Release-gated aarch64 artifact built on `ubuntu-24.04-arm` alongside the x86_64 build (ADR 0024, un-defers ADR 0013).
* **Architecture-Aware Self-Update**: The updater derives its asset name from the host arch and refuses a release lacking the matching-arch artifact.
* **Flathub Prep**: Manifest runtime bumped `org.gnome.Platform 46` → `49`; AppStream `<content_rating>`, `<developer>`, and `<screenshots>` added. Submission remains a maintainer decision.

---

### 📊 Play Insights in the Library
* **30/90/365-Day Ranges**: Insights render in the library pane with a range selector, lazy IntersectionObserver load, and debounced reload.
* **Top-Games Deep Links**: Clicking a top game opens its detail.

---

### 🩹 Fixes
* `gamescope_preset`/`mangohud_enabled`/`show_insights` settings now persist instead of being silently dropped by the settings whitelist.
* Context-menu "add to playlist" and Big Box "Achievements" no longer throw ReferenceErrors.
* Chosen UI language survives reload via `openbox-locale` localStorage; `app:state-refreshed` is dispatched (debounced) from `library.js refresh()`.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.2...v1.8.0
