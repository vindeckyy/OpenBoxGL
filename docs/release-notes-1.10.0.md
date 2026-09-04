# OpenBox v1.10.0: Solid and Fast

**OpenBox v1.10.0** is a consolidation release. It hardens what 1.9.0 introduced: SQLite answers now say which path served them, library sync shows its conflicts instead of resolving them silently, the LaunchBox importer resolves emulators and remaps Windows paths, Game Night resumes interrupted rounds, and four reliability scenarios move from manual checks to automated tests.

---

### Observable SQLite

* **Every Answer Labeled**: Facets and `/api/v2/library/search` report `source` (`sqlite` or `json`), `parity_ok`, and per-path `timings_ms` when `OPENBOX_ENABLE_SQLITE_READ=1` is on. A mismatch logs one counts-only warning and serves JSON (ADR 0037).
* **Opt-In Filtered Queries**: `OPENBOX_ENABLE_SQLITE_QUERY=1` runs platform, genre, favorite, hidden, installed, and offset filters as one indexed query. Everything off stays byte-identical to before, and `GET /api/library` stays JSON by design.

---

### Conflict-Visible Sync

* **Review List**: Pulls append `conflicts[]` with both timestamps, the last-writer-wins winner, and the fields that differ; the settings dialog lists them next to a notice that media stays per-device (ADR 0038).
* **Mass-Delete Guard**: A pull that would delete more than 10% of the local library answers 409 `SYNC_NEEDS_CONFIRM` with counts and changes nothing until resent with `confirm: true`.
* **Tombstone Expiry**: Publish drops tombstones older than 90 days and reports the pruned count; re-added games still clear their tombstone.

---

### Completable LaunchBox Migration

* **Resolve Step**: `POST /api/v2/import/launchbox/resolve` maps LaunchBox emulator ids to OpenBox adapters with registry validation (unknowns stay reported) and remaps Windows path prefixes to a shelf directory. Leftovers become `needs_path` shelf rows, never broken launchers or shell strings (ADR 0039).
* **Streaming Previews**: XML parsing streams with 5k-row pagination, so 20k-game exports stay bounded; re-resolve is safe and drift is rejected explicitly.

---

### Game Night Polish

* **Resume Offer**: A queue interrupted mid-round offers to resume instead of restarting silently, and every queue build shows an "Excluded N of M" transparency line.
* **Per-Game MangoHud**: Each game can force the overlay on or off, or inherit the global toggle. Video snaps default on with a Settings toggle that honors reduced-motion, preload only the visible snap plus one, and never leave BGM stuck quiet.

---

### Discover Follow-Ups

* **Picker**: "Again" sends the last shown ids as `excluded_ids[]` and the server enforces the exclusion, so repeats are impossible; reasons are localized.
* **Constellation**: `?focus=<game_id>&depth=1|2` renders the neighborhood around one game (double-click to focus, double-click empty canvas to clear); unknown ids return an honest empty graph.
* **Mastery & Timeline**: A "local progress only" notice appears when the RetroAchievements cache is unavailable, and Timeline basenames handle Windows separators.
* **POST-Payload Lint**: A new gate requires every POST handler to take the parsed `payload` argument, making the v1.9.0 pick-hang class impossible.

---

### Hardening & Verification

* **95% Changed-Line Gate**: Every lane must land at least 95% changed-line coverage on its own diff, with no waivers (ADR 0040).
* **Reliability Promotions**: Double-launch lease (409 `SESSION_ALREADY_STARTING`), offline sync retry, 401 credential guidance, dialog-delete rebinding, and SIGTERM shutdown draining are all covered by automated tests.
* **Deferred**: Frontend decomposition (splitting the large JS and parity modules with zero behavior change) moves to 1.11 to protect the gate.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.9.0...v1.10.0
