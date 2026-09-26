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
| 8 | Two quick launches of the same game | One atomic reservation wins; the competing request receives `LAUNCH_ALREADY_ACTIVE`, and a validation failure releases the reservation so a retry can succeed | Tested (`tests/test_launch_dedupe_http.py`) |
| 9 | Emulator install fails mid-download | Temp staging is cleaned; no half-installed emulator dir; retry works | Tested (test_emulators.py) |
| 10 | Non-UTF8 filename in game path | Import survives; UI renders replacement chars; launch still works | Tested (test_importers.py test_parallel_scanner_non_utf8_names) |
| 11 | Offline metadata sync | LBDB sync surfaces a clean connection error in the job panel and the sync button stays armed for retry | Tested (test_metadata.py offline URLError case) |
| 12 | Steam library on read-only mount | Import reports the permission error with the actual path | Tested (test_importers.py read-only steamapps case; import response carries an `errors` array) |
| 13 | Wrong RetroAchievements / EmuMovies credentials | 401/403 surfaces as "RetroAchievements rejected those credentials", not a generic error | Tested (test_retroachievements.py HTTPError 401 case) |
| 14 | Webhook target down | Retries with backoff, then a notification carries the last error | Tested (test_backend_hardening.py webhook cases) |
| 15 | GitHub rate-limited update check | Update endpoint degrades to a readable error; UI shows last check time | Tested (test_updates.py) |
| 16 | Huge archive with thousands of members | Extraction enforces MAX_ARCHIVE_MEMBERS; error names the cap; cache dir stays bounded | Tested (test_archives.py) |
| 17 | Manual PDF inside a password-protected zip | find_archive_manual returns None and records the no-manual note; no crash | Tested (test_metadata.py) |
| 18 | Duplicate covers from two metadata sources | Media dedupe reports per-field counts; cleanup removes the extras | Tested (test_changelog_features.py) |
| 19 | Broken symlink as media path | /api/media 404s cleanly, no traceback | Tested (test_media_paths.py approved_media_path symlink rejection) |
| 20 | 20,000-game library | Grid virtualizes; sidebar counts compute once; search debounces; picker and constellation stay within their measured gates | Gated (blocking CI job `perf-20k`: `scripts/perf_bench.py --sizes 10000,20000`, API/write/picker/constellation p95 budgets in `docs/development/PERF.md`) |
| 21 | 300+ character game names | Ellipsis everywhere; no layout break | Tested (ui_smoke hardening.longName: 400-char card truncates, no page overflow) |
| 22 | Selected game deleted while a dialog is open | Dialogs close or rebind; no stale selectedId crash | Tested (ui_smoke hardening.deleteSelected: details open, remove game, selectedId cleared) |
| 23 | Rapid filter switching during render | No half-rendered state; render reads one consistent AppState snapshot | Tested (ui_smoke hardening.filter: 10 rapid platform toggles, filteredGames consistent) |

## Windows channel

Since [ADR 0048](adr/0048-windows-port.md) OpenBox runs on Windows x86_64, and these
rows cover that channel specifically. They follow the same discipline as the rows
above: `Tested` means a test that actually **runs** on Windows (verified with
`python -B tests/test_platform_compat.py`, where 17 of 78 cases skip as
POSIX-only), not merely that a test file exists. Scenarios that need a machine or
an OS policy the suite cannot reach are `Manual` with the procedure written here,
and known limits are `Documented`.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 24 | Library data directory resolution | Resolves under `%LOCALAPPDATA%`, and the install root matches the `%LOCALAPPDATA%\OpenBox\share\openbox` layout `install.ps1` creates | Tested (test_platform_compat.py) |
| 25 | Start Menu shortcut location | The shortcut is written where Explorer looks for it | Tested (test_platform_compat.py) |
| 26 | Launch command with spaces, quotes, and non-ASCII characters | Arguments survive MSVCRT parsing: quoted groups stay together, doubled and escaped quotes round-trip, and empty/unterminated quotes are handled | Tested (test_platform_compat.py) |
| 27 | Stored command does not round-trip byte-for-byte across platforms | Deliberate: POSIX uses `shlex`, Windows uses MSVCRT. The guarantee is "launches correctly", not "argv is identical" | Documented (ADR 0048; `docs/PARITY.md` Windows channel) |
| 28 | A liveness probe must never kill the running game | `os.kill(pid, 0)` is `TerminateProcess` on Windows, so liveness always goes through `process_alive`; invalid and dead pids report false rather than signalling | Tested (test_platform_compat.py) |
| 29 | Pausing a running game | Suspend stops the target and resume restarts it; invalid pids are rejected instead of raising | Tested (test_platform_compat.py) |
| 30 | Closing a game that spawned children | Terminating the tree kills the live child, with a PID fallback when the group is already gone | Tested (test_platform_compat.py) |
| 31 | Signalling must never reach process group 0 or 1 | Group targeting refuses init and the caller's own group, and refuses PID 1 outright | Tested (test_platform_compat.py) |
| 32 | Two OpenBox processes at once (Windows lock semantics) | The file lock serializes writes. Note the platform difference: `msvcrt` byte-range locks have no shared mode, so the shared-lock case is POSIX-only and is covered by row 3 rather than duplicated here | Tested (test_platform_compat.py) |
| 33 | Steam and Epic library discovery on Windows | Steam roots return real directories; Epic manifests are read from the Windows manifest folder, and a missing or failing glob yields nothing rather than raising | Tested (test_platform_compat.py) |
| 34 | Profile-owned files are treated as private | Files owned by the current user profile pass the privacy check that POSIX mode bits drive | Tested (test_platform_compat.py) |
| 35 | Process identity probes | Name, command line, and cwd resolve for a live child, uncoercible pids are rejected, and `/proc`-only semantics stay POSIX-only | Tested (test_platform_compat.py) |
| 36 | Emulator executable detection | Launchable files are recognized by suffix; `native_exe_windows` is required on every definition so adapter detection works | Tested (test_platform_compat.py; `scripts/check_emulator_defs.py`) |
| 37 | Browser fallback when no native host is present | Browser candidates resolve to launchable programs, an absolute install wins over a bare name, and absence is reported rather than raised | Tested (test_platform_compat.py) |
| 38 | Home directory missing from the environment | Home resolution and `.env` file roots degrade instead of raising | Tested (test_platform_compat.py) |
| 39 | WebView2 runtime not installed | The launchers fall back to the browser app window rather than failing. Verify on a Windows image without the Evergreen runtime: run `openbox.cmd` and confirm a browser app window opens and the UI is fully usable | Manual (requires a host without the WebView2 runtime; the release notes document `--web` as the supported path) |
| 40 | `native_host.exe` blocked by SmartScreen, Defender, or Mark of the Web | The launcher reports why it fell back instead of silently opening a tab. Verify by unblocking the binary (`Unblock-File`) and re-downloading it to reproduce the blocked state | Manual (OS trust policy is not reproducible in CI) |
| 41 | Second instance with the same data directory | The named pipe created with `FILE_FLAG_FIRST_PIPE_INSTANCE` is the atomic guard; the second process forwards `focus` and the first window raises. Verify by launching two instances against one data dir | Manual (CI only compiles `native_host_win.c`; it does not drive two windows) |
| 42 | `openbox://` deeplink with the app closed | A cold-start browser dispatch reaches the running or newly started window. Verify by closing OpenBox, then opening an `openbox://` link | Manual (requires a registered protocol handler) |
| 43 | Update applied while OpenBox is running | The detached PowerShell applier completes the swap after the server stops, keeping `openbox.previous`; a refusal to replace a non-installed tree is reported | Manual (requires an installed tree; the update path is exercised end to end by the release job, not by unit tests) |
| 44 | Path longer than 260 characters | Behavior depends on the system long-path opt-in; without it, an explicit error naming the path is expected rather than a truncated launch | Documented (Windows MAX_PATH; long paths are not normalized by `platform_compat` in 1.14.0) |
