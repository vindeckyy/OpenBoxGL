# Reliability scenarios

Each row is a failure mode a real user can hit. Status means:

- **Tested**: covered by an automated test or script, run in the gate.
- **Manual**: verified by a documented manual procedure per release.
- **Documented**: known behavior with explicit UI/docs guidance; no code change planned.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 1 | library.json truncated mid-write | `.bak` recovers; rolling snapshots offer older states; corrupt primary + corrupt backup shows the recovery dialog, never a silent wipe | Tested (test_state_v4.py, test_perf_writes.py) |
| 2 | Full disk during state write | Atomic write fails cleanly; error names the data dir and hints free space; no partial primary file | Tested (test_perf_writes.py `test_write_failure_keeps_primary_and_backup_consistent` and `test_atomic_recovery_on_write_interruption`: both simulate a write failure and assert the primary and backup stay consistent) |
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
| 14 | Webhook target down | Retries with backoff, then a notification carries the last error | Tested (test_four_features.py `test_delivery_uses_injected_clock_for_retry_timing` drives the retryable-429 path and asserts the backoff sleep runs; the terminal `on_result` carrying the last error is covered by test_sse.py `test_ns_fallback_and_notification_errors`, which calls `sse._emit_webhook_failure` on the missing-emitter, missing-fallback, and raising-emitter paths) |
| 15 | GitHub rate-limited update check | Update endpoint degrades to a readable error; UI shows last check time | Tested (test_updates.py) |
| 16 | Huge archive with thousands of members | Extraction enforces MAX_ARCHIVE_MEMBERS; error names the cap; cache dir stays bounded | Tested (test_archives.py) |
| 17 | Manual PDF inside a password-protected zip | find_archive_manual returns None and records the no-manual note; no crash | Documented (no automated test: `find_archive_manual` in `metadata.py` catches `OSError`, `ValueError`, `RuntimeError`, and `zipfile.BadZipFile` and returns None, but nothing exercises the encrypted-zip path) |
| 18 | Duplicate covers from two metadata sources | Media dedupe reports per-field counts; cleanup removes the extras | Tested (test_shared_media.py shared-cover and duplicate-path cases) |
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
| 44 | Path longer than 260 characters | Behavior depends on the system long-path opt-in; without it, an explicit error naming the path is expected rather than a truncated launch | Documented (Windows MAX_PATH; long paths are not normalized by `platform_compat` in 1.15) |

## Contributor channel

These are not things a user can hit, but they are the failures most likely to
reach `master` unnoticed, because a developer on any platform sees a green
local gate while a specific CI job is already red. Each row is here for the same
reason as the rows above: the failure mode is a real one that has actually
happened, and its status says what stops it happening again.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 45 | A file carrying a shebang loses its exec bit | The gate fails pre-push instead of after. Windows has no exec bit, so ruff's `EXE001` cannot see this and neither can the `windows-latest` job; only the git index records the mode on every platform. This went red on `master` for 12 commits | Tested (`scripts/check_exec_modes.py`, wired into `check_tests.py` and the Linux `gate` job; covered by `tests/test_ci_gates.py`) |
| 46 | A UI fixture pins an absolute date against a rolling window | The fixture ages out of its own window and fails for a reason unrelated to the code under test. The activity smoke fixture did exactly this on 2026-09-23, once it was 30 days older than its hardcoded timestamps | Tested (`tests/test_feature_contracts.py` `ClockCouplingTest` writes a date-pinned fixture and asserts `check_clock_coupling.py` reports it; the marker and ledger live in `scripts/contracts/clock_coupling.json`) |
| 47 | A wall-clock budget is measured once, cold | The reading charges the algorithm for the interpreter's lazy initialization and the runner's scheduling noise, so it fails on a loaded shared runner and passes on every developer machine. The 10k-ROM import budget sat at a 231ms median against a 250ms budget for exactly this reason | Tested (`tests/test_ci_gates.py` `test_import_benchmark_is_warmed_and_best_of_n` pins the warm-up of both functions, the best-of-N assertion, and the unchanged 250ms budget; proven behaviorally by extracting the pre-change file and running both forms under escalating CPU load, where the old form fails at 8 and 10 threads and the new form passes at up to 16) |
| 48 | The Windows CI runner gives a suite a single chance | A timing flake is reported as a hard failure, so only CI can see it and every developer machine stays green. `run_windows_tests.py` ran each suite once while the local gate allows three attempts | Tested (`scripts/run_windows_tests.py` retries like `check_tests.py`, marks a retried suite as passed, and still prints the first attempt's failure so a real intermittent bug surfaces; covered by `tests/test_windows_runner.py`) |

## Windows lifecycle

Added after the contributor rows so existing row numbers stay stable.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 49 | Uninstalling OpenBox on Windows | `scripts/uninstall.ps1` removes the install tree and its `openbox.previous` rollback copy, the user PATH entry, the Start Menu shortcut, and the `openbox://` registration, and nothing else: the library and settings in the data folder (shown in Settings > About) survive so a reinstall picks them up. `-WhatIf` previews it | Tested (test_uninstall_script.py) |

## 1.16.0

Added after the Windows lifecycle row so existing row numbers stay stable. Every
row here was written after the test it names existed (ADR 0064, rule 1).

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 50 | Restoring a backup that is years out of date | The restore button does not exist until the diff loads; the preview names every game the restore would remove, bring back and overwrite, with true totals and explicit truncation; a settings line appears when settings.json would move; a diff that fails offers a retry and no way to restore | Tested (`tests/test_restore_preview.py` pins the diff against a real `create_backup` fixture — set direction, per-field from/to detail, truncation with honest totals, and the settings manifest/archive distinction; `test_frontend_contract.py` `test_restore_cannot_be_armed_without_a_preview` pins the structure and `test_restore_preview_reads_the_restore_block_not_the_set_names` pins the vocabulary; ui_smoke `motion.restorePreview` drives the real panel through a success, a 500 and a retry, and asserts zero restore POSTs) |
| 51 | "How many of my games will launch?" on a 20,000-game library | The Launch Audit runs every game through the Launch Doctor in a cancellable background job and groups the results by root cause ("RetroArch is not installed: 412 games"), with a total ready/warning/blocked line, paginated member lists and one fix per cause. Each distinct Flatpak app id costs at most two `flatpak` spawns however many games use it | Tested (`tests/test_launch_audit.py` `test_two_thousand_games_spawn_at_most_two_flatpaks_per_app_id` and `test_the_spawn_count_does_not_grow_with_the_library` assert the spawn count with a counting fake `run`, and `test_the_filesystem_grant_is_still_decided_per_rom` proves the cache holds the permission text, not the per-ROM answer; ui_smoke `motion.launchAudit` drives the real panel) |
| 52 | The audit and the single-game Doctor disagree about a game | They cannot: the audit calls `run_preflight_checks` unchanged, and never mutates the library | Tested (`tests/test_launch_audit.py` `test_audit_membership_is_exactly_the_doctors_codes` compares every game's audit groups with a direct Doctor run; `test_the_job_reports_and_the_route_pages_members` asserts the library is byte-identical after a scan and that member pages are capped at 500) |
| 53 | A game deleted while it is running, then OpenBox restarted | No game's playtime moves; before 1.16.0 the hours were credited to whatever game was last in the library | Tested (`tests/test_playtime_attribution.py` `test_no_game_earns_the_deleted_sessions_hours` drives the real `finish_session` with the legacy `-1` index) |
| 54 | Importing a folder that already holds a hand-made `Game.m3u` beside its disc images | The user's playlist is left byte-identical and is not rewritten anywhere; its ROM is imported exactly once | Tested (`tests/test_import_playlist_safety.py` `test_the_users_playlist_is_byte_identical_after_the_import` and `test_the_referenced_rom_is_represented_exactly_once`) |

## 1.16.1

Added after the 1.16.0 section so existing row numbers stay stable. Every `Tested` row names a test that exists (ADR 0064, rule 1); `Manual` rows carry the procedure.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 55 | A stale window edits or trashes a game that moved | A request that names a stable game id which no longer exists is refused; it never falls back to the array index and touches a neighbouring game | Tested (tests/test_launch_phases.py) |
| 56 | A Flatpak probe hangs (wedged Flatpak or D-Bus session) | Status, open and setup probes give up after 5 s and count the emulator as not installed; no request blocks on them | Tested (tests/test_subprocess_timeouts.py) |
| 57 | A library entry's last-played stamp carries `Z` or an offset | `played recently`, idle and backup-age rules compare in local time and match it; a bulk edit rejects a last-played value it cannot parse | Tested (tests/test_parity_query.py, tests/test_platform_compat.py, tests/test_catalog.py) |
| 58 | A Flatpak emulator is installed but cannot read the game's folder | The fix grants read-only access to that one folder after confirmation, and Remove access reverses it; the grant never reinstalls the emulator and refuses the home folder itself and any id no bundled definition declares | Tested (tests/test_parity_flatpak_grant.py, tests/test_launch_doctor.py) |
| 59 | A bridge id with an apostrophe, backslash or line break (Linux host) | The id is spliced as one escaped JS literal, so the page's promise resolves instead of hanging | Tested (tests/test_native_host.py) |
| 60 | The window is hidden and shown again while a game runs | The session event stream reopens on return and the scheduled-rescan stream retries with backoff, so updates resume without a reload | Manual (procedure: start a game from OpenBox; hide the window for 30 s, then show it; the session card must update when the game exits, with no page reload. Repeat with the rescan stream by stopping and restarting the server; the rescan toast must return within a minute.) |
| 61 | A bulk media download finishes with failed games | "Retry failed" is enabled only when some games failed, and re-runs only those games | Manual (procedure: Library > Media manager; run a bulk download that fails for at least one game; confirm "Retry failed" enables, press it, and confirm only the failed games are reported again.) |
| 62 | A coalesced snapshot is flushed after a full commit | The older snapshot is discarded; the committed game is kept and no half-applied mutation persists after an exception | Tested (tests/test_perf_writes.py) |
| 63 | Snapshot rotation with tied modification times | The newest recovery point survives rotation; snapshots list newest first | Tested (tests/test_perf_writes.py) |
| 64 | A metadata record carries a full ISO release date | The year field stores the year alone and an existing year is kept unless overwrite is set | Tested (tests/test_metadata.py) |
| 65 | English-only notification text reaches a non-English user | Literal English `notify()` calls are counted and ratcheted; each migration to `t()` lowers the count in the same change | Tested (tests/test_notify_literals.py) |
| 66 | A game is addressed by its array index after a delete or sync | Every game-addressed request sends the stable id too; the ratchet keeps index-only requests from returning | Tested (tests/test_game_addressing_ratchet.py) |
| 67 | A bulk-edit or import writes a date the query engine cannot read | Rejected at the bulk-edit boundary; the parser is shared, and bare parsing is ratcheted down | Tested (tests/test_game_addressing_ratchet.py, tests/test_catalog.py) |
| 68 | A Flatpak RetroArch launch of a system core | The launch uses the sandbox's cores directory (ADR 0067); the Doctor names a missing core instead of passing a path the sandbox cannot read | Tested (tests/test_emulators.py, tests/test_launch_doctor.py) |
| 69 | A system with no definition: Genesis, Master System, Game Gear, Sega CD, PC Engine, Neo Geo Pocket, Atari 2600/7800/Lynx/Jaguar, WonderSwan, Virtual Boy, C64, MSX, Amiga, Dreamcast, 3DS, MS-DOS | Each has a definition that loads, builds a launch command, and names a verified core or Flathub id | Tested (tests/test_emulators.py) |
| 70 | Three open event streams, or a page load that opens a second one | One event stream per tab (ADR 0068); a second EventSource anywhere in the frontend fails the gate | Tested (tests/test_frontend_contract.py) |
| 71 | A stale window saves over a game that moved or was deleted | The save is refused with 400 and the library is unchanged; the same save with the right stable id is accepted | Tested (scripts/ui_smoke.cjs stale save) |
| 72 | A search that empties the grid shifts the page | The layout shift stays under the web-vitals threshold (0.1); the browser suite measures it | Tested (scripts/ui_smoke.cjs motion.cls) |
| 73 | First render and long tasks of a 10,000 or 20,000 game library in a browser | perf_bench.py --browser measures the first render, the long tasks and the search shift; a shift above 0.1 fails the bench when the browser ran | Manual (procedure: run python3 -B scripts/perf_bench.py --sizes 10000 --runs 3 --browser --no-gate with PUPPETEER_EXECUTABLE_PATH set to a Chrome build, then record the browser block in docs/development/PERF.md. Not run in CI.) |
| 74 | A folder grant made from the Launch Doctor must be removable | Remove access appears after a grant and runs the undo; the Doctor then checks the folder again | Manual (procedure: on a machine with Flatpak RetroArch, open a game whose folder the sandbox cannot read; click Grant access and confirm; the check clears. Click Remove access and confirm; `flatpak override --user --show org.libretro.RetroArch` must no longer list that folder.) |
| 75 | Remove Steam games deletes them with no way back | The games move to the Trash with their playlists and position, Restore brings them back, and a removal larger than the Trash cap moves only what fits and leaves the rest in the library | Tested (tests/test_parity_api.py) |
| 76 | The Launch check's blocked or warning totals change between two checks | One notice reaches the feed for the new result; a repeat of the same result adds none; the first check adds none | Tested (tests/test_launch_audit.py) |
| 77 | Many games wait on one Flatpak folder grant or one emulator install | The group shows one fix for all its games, the fix covers every game in it, and only that group's games are checked again after a grant | Manual (procedure: with a Flatpak emulator that cannot read two or more games' folders, open Settings > Launch readiness, click Grant access for the group, confirm; the group must clear after the re-check. Repeat with Install for an uninstalled emulator.) |
| 78 | The launch readiness badges are turned off in Settings | The setting is saved, the grid hides the badges while it is off, and it stays off after a reload | Manual (procedure: Settings > Appearance > uncheck 'Show launch readiness badges on the grid'; the Won't launch and Needs attention badges disappear from the grid; reload the page; they stay hidden. Re-check the box to restore them.) |
| 79 | A group of games needs its moved files found | 'Find moved files' lists only that group's games; a group the report no longer has is refused, not widened | Tested (tests/test_parity_repair.py) |
| 80 | A RetroArch core is missing for a game | The picker lists the installed cores, the game launches with the core it picks, and 'Use the default core' restores the definition's core. The builder and the route are covered by tests/test_emulators.py (CoreChoiceTests); the picker itself is manual | Manual (procedure: remove or rename a core in the RetroArch cores folder so the Doctor reports RETROARCH_CORE_MISSING; click Choose core, pick another installed core; the Doctor must clear and the launch command must name the picked core. Then click 'Use the default core' and confirm the command names the definition's core again.) |
| 81 | A .bin, .cue or .iso file belongs to several systems | The file waits for a platform choice; importing it before a choice is refused; the choice must be one of the systems that declare its extension | Tested (tests/test_parity_setup_preview.py) |
| 82 | A light theme shows a dark native window before the page paints | Both native hosts paint the saved theme's background at launch; a missing or malformed file keeps #11100e | Tested (tests/test_native_background.py) |
| 83 | A definition pack written for a newer schema is installed | The pack is refused with the schema it needs; schema 1 and 2 install | Tested (tests/test_parity_emulator_defs_update.py) |
| 84 | A search adds filter chips and pushes the panels below | The chip row stays one line, so the drop zone and Play Insights do not move; the search layout shift is 0 on warm runs and under 0.1 on the cold run | Manual (procedure: PUPPETEER_EXECUTABLE_PATH=<chrome> python3 -B scripts/perf_bench.py --sizes 10000 --browser --runs 3 --no-gate; the browser block's search_cls_max must be under 0.1, and the raw runs must read 0 after the first.) |
| 85 | The emulator-definition pack is published for the update channel | Each tag builds, signs and verifies community-defs.tar.gz and uploads it with its signature and index.json to the emulator-defs release | Manual (procedure: after a tagged CI run with the release key, confirm the emulator-defs release lists community-defs.tar.gz, community-defs.tar.gz.sig and index.json, then run python3 -B scripts/verify_release.py --key openbox-release.pub community-defs.tar.gz community-defs.tar.gz.sig on the downloaded files.) |
| 86 | A group's fix is applied to many games and the launch check must stay correct | Only the group's games are checked again; the merged report equals a full audit of the library, and a stale report is refused | Tested (tests/test_launch_audit.py) |
