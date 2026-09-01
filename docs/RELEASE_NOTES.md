# OpenBox v1.7.2: Localization, Scale Foundation & Deck Polish

**OpenBox v1.7.2** introduces full internationalization, an optional SQLite read model for large libraries, gamescope presets and MangoHud for Steam Deck users, BIOS SHA1 drift detection, and a backup diff endpoint.

---

### 🌍 Full Localization
* **5 Languages**: Complete translations for English, Spanish, German, French, and Portuguese via JSON locale files in `locales/`.
* **data-i18n System**: HTML elements use `data-i18n` attributes; JS uses `t(key)` from `static/i18n.js` with automatic fallback to English.
* **Live Language Switching**: Settings → Interface language re-translates the UI without a page reload.
* **Gate Coverage**: `scripts/check_i18n.py` enforces 100% key coverage across all locale files; wired into `make check`.

---

### 📦 SQLite Read Model (Optional)
* **Behind a Flag**: `OPENBOX_ENABLE_SQLITE_READ=1` enables an alternative read path using stdlib `sqlite3`.
* **FTS5 Search**: Full-text search via FTS5 with automatic LIKE fallback when FTS5 is unavailable.
* **Indexed Queries**: Filtered lookups on platform, genre, favorite, hidden, installed with limit/offset pagination.
* **GROUP BY Facets**: Facet computation via SQL GROUP BY for platform, genre, developer, and more.
* **Zero Impact When Off**: Disabled by default; all methods are no-ops and the JSON read path is unchanged.

---

### 🎮 Deck Polish: Gamescope Presets & MangoHud
* **8 Gamescope Presets**: Steam Deck, HD, 1080p, 1440p, 4K, integer scale, stretch, and borderless window profiles.
* **MangoHud Toggle**: Enable the MangoHud performance overlay from Settings → Controller; `MANGOHUD=1` is set on game launch.
* **Controller Bench**: Settings → Controller tab with a live gamepad SVG visualization.

---

### 🩺 Emulator Health & BIOS Drift Detection
* **BIOS SHA1 Drift**: Launch Doctor now reports `BIOS_SHA1_DRIFT` when a BIOS file exists but its SHA1 hash doesn't match the expected value in `emulator_defs/*.yaml`.
* **Health Badges**: CSS badge classes (ok/warn/fail) with design tokens in `static/app.css` and all 5 themes.
* **Registry Health API**: `GET /api/v2/emulators/registry?health=1` returns `bios_ok`, `firmware_ok`, and `core_ok` per adapter.

---

### 🏷️ Smart Collections & Backup Diff
* **Visual Chip Builder**: `rules_to_chips()` and `chips_to_rules()` convert between filter preset rules and UI chip descriptors for visual collection editing.
* **Backup Diff API**: `GET /api/v2/backup/diff?archive=<name>` compares the current library against a backup archive, returning added/removed/changed game IDs and settings change status.

---

### 🎨 Design System
* **15 New Tokens**: 6 gamepad/controller tokens + 9 health badge tokens in `static/app.css` `:root`.
* **5 Stock Themes Updated**: All new tokens covered across Cinema Marquee, Harbor Light, Midnight Circuit, Nordic Mist, and Phosphor Terminal.

---

### 📋 Gates & Release
* New runtime module `pkg/state/sqlite_readmodel.py` (90% coverage, floor 85%).
* 7 new GET routes (1 static + 5 locale JSONs + 1 backup diff v2).
* `scripts/check_i18n.py` wired into `make check` as a strict gate.
* ADRs: `0014-sqlite-read-model.md`, `0015-i18n-system.md`.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.1...v1.7.2
