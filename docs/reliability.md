# Reliability scenarios

Each row is a failure mode a real user can hit. Status means:

- **Tested**: covered by an automated test or script, run in the gate.
- **Manual**: verified by a documented manual procedure per release.
- **Documented**: known behavior with explicit UI/docs guidance; no code change planned.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 1 | library.json truncated mid-write | `.bak` recovers; rolling snapshots offer older states; corrupt primary + corrupt backup shows the recovery dialog, never a silent wipe | Tested (test_state_v4.py, test_perf_writes.py) |
| 2 | Full disk during state write | Atomic write fails cleanly; error names the data dir and hints free space; no partial primary file | Tested (test_backend_hardening.py) |
| 3 | Two OpenBox processes at once | Filesystem lock serializes writes; second instance operates read-mostly without corrupting state | Tested (test_perf_state.py lock cases) |
| 4 | State written by a newer version | Unknown fields are preserved (schema keeps unknown keys); future schema version surfaces a clear upgrade message, not a 500 loop | Tested (test_state_v4.py unknown-field cases) |
| 5 | Game binary disappears between render and launch | Launch fails with the concrete missing path, session error names it | Tested (test_sessions.py) |
| 6 | Game spawns children then exits fast | finish_session records the parent exit without killing unrelated process groups | Tested (test_sessions.py) |
| 7 | Game still running when OpenBox closes | SIGINT/SIGTERM triggers graceful stop of sessions; shutdown drains webhooks | Tested (web_app stop() + test_sessions.py) |
| 8 | Two quick launches of the same game | Launch button disables while starting; second click is a no-op until the first resolves | Manual (procedure: rapid double-click Play in test_sessions.py launch dedupe) |
| 9 | Emulator install fails mid-download | Temp staging is cleaned; no half-installed emulator dir; retry works | Tested (test_emulators.py) |
| 10 | Non-UTF8 filename in game path | Import survives; UI renders replacement chars; launch still works | Documented |
| 11 | Offline metadata sync | LBDB sync surfaces an offline state in the job panel with retry | Manual (procedure: disconnect network, trigger metadata sync, verify job panel offline + retry) |
| 12 | Steam library on read-only mount | Import reports the permission error with the actual path | Manual (procedure: chmod 500 mount, import, verify sanitized error) |
| 13 | Wrong RetroAchievements / EmuMovies credentials | 401 surfaces as "check credentials in settings", not a generic error | Manual (procedure: set bad credentials, invoke /api/ra/settings, verify 401 message) |
| 14 | Webhook target down | Retries with backoff, then a notification carries the last error | Tested (test_backend_hardening.py webhook cases) |
| 15 | GitHub rate-limited update check | Update endpoint degrades to a readable error; UI shows last check time | Tested (test_updates.py) |
| 16 | Huge archive with thousands of members | Extraction enforces MAX_ARCHIVE_MEMBERS; error names the cap; cache dir stays bounded | Tested (test_archives.py) |
| 17 | Manual PDF inside a password-protected zip | find_archive_manual returns None and records the no-manual note; no crash | Tested (test_metadata.py) |
| 18 | Duplicate covers from two metadata sources | Media dedupe reports per-field counts; cleanup removes the extras | Tested (test_changelog_features.py) |
| 19 | Broken symlink as media path | /api/media 404s cleanly, no traceback | Tested (test_media_paths.py approved_media_path symlink rejection) |
| 20 | 20,000-game library | Grid virtualizes; sidebar counts compute once; search debounces | Manual (perf bench with 10k gzip <50ms, 20k artifact under OPENBOX_PERF_FULL=1) |
| 21 | 300+ character game names | Ellipsis everywhere; no layout break | Manual (procedure: game name 400 chars, verify grid ellipsis, detail ellipsis) |
| 22 | Selected game deleted while a dialog is open | Dialogs close or rebind; no stale selectedId crash | Manual (procedure: open game dialog, delete via bulk, verify dialogs close) |
| 23 | Rapid filter switching during render | No half-rendered state; render reads one consistent AppState snapshot | Manual (procedure: rapid filter toggle 10x, verify no half-render) |
