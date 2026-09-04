# OpenBox v1.9.0: Look, Discover, Play

**OpenBox v1.9.0** introduces adaptive cover theming, a smart "What should I play?" picker, a Library Constellation relationship graph, an OpenBox Wrapped year-in-review report with a session Timeline, a Mastery Map completionist dashboard, and a Game Night Big Box party mode.

---

### Mood Match — Adaptive Cover Theming
* **Live Palette**: Selecting a game extracts a 5-color palette (primary, ink, secondary, glow, tint) from its cover with a fast 4×4×4 RGB bin quantizer and applies it to the selected card, detail hero, play button hover, and Big Box background (ADR 0026).
* **Off by Default**: Toggles in Settings → Appearance (`mood_match_enabled`/`mood_match_bigbox`); decorative accents only, text and focus tokens untouched.

---

### "What Should I Play?" Picker
* **Scored Suggestions**: Pick by available time, mood (action/chill/story/retro/party), familiarity (new/favorite), and player count via `POST /api/v2/library/pick` (`pkg/parity/parity_picker.py`, ADR 0028).
* **Reasons**: Every pick explains itself ("You added this 2 years ago and never launched it"); Launch / Details / Again actions plus a "Just surprise me" fallback.

---

### Library Constellation
* **Star Map**: Tools → Constellation renders a pan/zoomable canvas graph of series, developer, publisher, genre, platform-family, and co-play edges via `GET /api/v2/library/constellation` (ADR 0027).
* **Deterministic & Capped**: Nodes ranked by playtime (200/400/800/1000), one strongest edge per pair, chunked spring-electric layout; clicking a node selects it in the library.

---

### Wrapped + Replay Timeline
* **Your Year in Games**: Insights → Wrapped opens a printable report with playtime, sessions, streaks, progress, top game/platform/genre, oldest played, and busiest month via `GET /api/v2/insights/wrapped?year=YYYY` (ADR 0029).
* **Timeline Tab**: History → Timeline groups sessions by day with covers and recording badges via `GET /api/v2/history/timeline?days=90`. Privacy-safe by construction: names, covers, and aggregates only.

---

### Mastery Map
* **Completionist Dashboard**: Tools → Mastery shows stacked per-platform and per-decade bars over local progress states with a RetroAchievements column fed exclusively from the on-disk cache — zero new network calls (ADR 0030).
* **Route**: `GET /api/v2/insights/mastery`; clicking a segment filters the library to that platform.

---

### Game Night Party Mode
* **Couch Flow**: Big Box → Game Night builds a multiplayer queue from player count and session length, spins a wheel for the round winner, shows an "Up next" strip, and persists rounds across restarts (`POST`/`GET /api/v2/party/queue`, `POST /api/v2/party/next`, ADR 0031).
* **Controls**: Gamepad via the existing `pollGamepads` edge detection plus keyboard fallback (arrows/Enter/N/Escape).

---

### Fixes
* `POST /api/v2/library/pick` no longer hangs on a live server: POST handlers take the parsed `payload` argument instead of re-reading the consumed request body (ADR 0031).

---

### Architecture & Performance
* **Parity shim cleanup**: All 28 root-level `parity_*.py` shims deleted; `MetaPathFinder` is the sole flat-import bridge (ADR 0003).
* **Central dependency registry**: `pkg/state/_deps.py` replaces 4 private `_ns()` helpers (ADR 0009).
* **SQLite read model graduated**: `OPENBOX_ENABLE_SQLITE_READ=1` now serves facets via GROUP BY and adds `GET /api/v2/library/search` with FTS5. Default off = no behavior change (ADR 0032).

---

### LaunchBox Migration
* Import your LaunchBox library via `POST /api/v2/import/launchbox/preview` and `/apply`. Emulator mappings are reported for manual resolution (ADR 0033).

---

### Big Box Video Snaps
* Stage mode shows looping gameplay videos with 600ms debounce, BGM ducking, and reduced-motion support (ADR 0034).

---

### Library Sync
* `POST /api/v2/library/sync/publish` and `/pull` sync the full library via a mounted folder with tombstones for deletions (ADR 0035).

---

### Manual/Shelf Entries
* `POST /api/v2/library/manual-entry` adds games without a local file path — physical media, board games, console games (ADR 0036).

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.8.0...v1.9.0
