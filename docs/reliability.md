# Reliability scenarios

Each row is a failure mode a real user can hit. Status means:

- **Tested**: covered by an automated test or script, run in the gate.
- **Partial**: the tested part is named; an explicit gap remains (honest gap, not a promise).
- **Manual**: verified by a documented manual procedure per release.
- **Documented**: known behavior with explicit UI/docs guidance; no code change planned.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 1 | library.json truncated mid-write | `.bak` recovers; rolling snapshots offer older states; corrupt primary + corrupt backup shows the recovery dialog, never a silent wipe | Tested (test_state_v4.py, test_perf_writes.py) |
| 2 | Full disk during state write | Atomic write fails cleanly; reads never rewrite state, so a GET on a full disk still succeeds (P0-10). The data-dir + free-space error hint is not implemented yet | Partial (tested: test_state_v4.py no-write-on-read, test_perf_writes.py in-memory migration; gap documented) |
| 3 | Two OpenBox processes at once | Writes take an exclusive filesystem lock; reads take a shared lock (`LOCK_SH`, P1-7) so readers run concurrently and never observe a half-applied write | Tested (test_state_read_locks.py lock cases, test_state_snapshot.py) |
| 4 | State written by a newer version | Unknown fields are preserved (schema keeps unknown keys); future schema version surfaces a clear upgrade message, not a 500 loop | Tested (test_state_v4.py unknown-field cases) |
| 5 | Game binary disappears between render and launch | Launch fails with the concrete missing path, session error names it | Tested (test_sessions.py) |
| 6 | Game spawns children then exits fast | finish_session records the parent exit without killing unrelated process groups | Tested (test_sessions.py) |
| 7 | Game still running when OpenBox closes | SIGINT/SIGTERM triggers graceful stop of sessions; shutdown drains webhooks | Tested (web_app stop() + test_sessions.py) |
| 8 | Two quick launches of the same game | One atomic reservation wins; the competing request receives `LAUNCH_ALREADY_ACTIVE`, and a validation failure releases the reservation so a retry can succeed | Tested (`tests/test_launch_dedupe_http.py`) |
| 9 | Emulator install fails mid-download | Temp staging is cleaned; no half-installed emulator dir; retry works | Tested (test_emulators.py) |
| 10 | Non-UTF8 filename in game path | Import survives; UI renders replacement chars; launch still works | Tested (test_importers.py test_parallel_scanner_non_utf8_names) |
| 11 | Offline metadata sync | LBDB sync surfaces a clean connection error in the job panel and the sync button stays armed for retry | Tested (test_metadata.py offline URLError case) |
| 12 | Steam library on read-only mount | Import reports the permission error with the actual path | Tested (test_importers.py read-only steamapps case; import response carries an `errors` array) |
| 13 | Wrong RetroAchievements / EmuMovies credentials | 401/403 surfaces as "RetroAchievements rejected those credentials", not a generic error | Tested (test_retroachievements.py HTTPError 401 case) |
| 14 | Webhook target down | Retries with backoff, then a notification carries the last error | Tested (test_backend_hardening.py webhook cases) |
| 15 | GitHub rate-limited update check | Update endpoint degrades to a readable error; UI shows last check time | Tested (test_updates.py) |
| 16 | Huge archive with thousands of members | Extraction enforces MAX_ARCHIVE_MEMBERS; error names the cap; the extraction cache prunes to a bounded tree budget (P1-10) | Tested (test_archives.py, test_archive_cache.py) |
| 17 | Manual PDF inside a password-protected zip | find_archive_manual returns None and records the no-manual note; no crash | Tested (test_metadata.py) |
| 18 | Duplicate covers from two metadata sources | Media dedupe reports per-field counts; cleanup removes the extras | Tested (test_changelog_features.py) |
| 19 | Broken symlink as media path | /api/media 404s cleanly, no traceback | Tested (test_media_paths.py approved_media_path symlink rejection) |
| 20 | 20,000-game library | Grid virtualizes; sidebar counts compute once; search debounces; picker and constellation stay within their measured gates | Gated (blocking CI job `perf-20k`: `scripts/perf_bench.py --sizes 10000,20000`, API/write/picker/constellation p95 budgets in `docs/development/PERF.md`) |
| 21 | 300+ character game names | Ellipsis everywhere; no layout break | Tested (ui_smoke hardening.longName: 400-char card truncates, no page overflow) |
| 22 | Selected game deleted while a dialog is open | Dialogs close or rebind; no stale selectedId crash | Tested (ui_smoke hardening.deleteSelected: details open, remove game, selectedId cleared) |
| 23 | Rapid filter switching during render | No half-rendered state; render reads one consistent AppState snapshot | Tested (ui_smoke hardening.filter: 10 rapid platform toggles, filteredGames consistent) |
| 24 | A read hits state that needs normalizing | Reads never rewrite library.json (P0-10); they return the normalized view and the next mutation persists it | Tested (test_state_v4.py `test_load_readonly_does_not_write_when_normalizing`, test_perf_writes.py `test_ensure_loaded_migrates_in_memory_without_writing`) |
| 25 | An edit transaction overlaps a read | Reads take a consistent snapshot under the store lock; they never observe a half-applied edit or raise `dictionary changed size during iteration` (ADR 0049) | Tested (test_state_snapshot.py, test_state_read_locks.py) |
| 26 | library.json corrupt at boot | The server starts in recovery mode: normal routes answer `503 STATE_UNAVAILABLE` and `POST /api/state/recover` restores last-known-good or a snapshot | Tested (test_web_app_boot.py `test_main_starts_in_recovery_mode_with_corrupt_state`, test_health_robustness.py) |
