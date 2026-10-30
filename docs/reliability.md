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
| 82 | A light theme shows a dark native window before the page paints | Both native hosts paint the saved theme's background at launch; a missing or malformed file keeps #0F0E0C | Tested (tests/test_native_background.py) |
| 83 | A definition pack written for a newer schema is installed | The pack is refused with the schema it needs; schema 1 and 2 install | Tested (tests/test_parity_emulator_defs_update.py) |
| 84 | A search adds filter chips and pushes the panels below | The chip row stays one line, so the drop zone and Play Insights do not move; the search layout shift is 0 on warm runs and under 0.1 on the cold run | Manual (procedure: PUPPETEER_EXECUTABLE_PATH=<chrome> python3 -B scripts/perf_bench.py --sizes 10000 --browser --runs 3 --no-gate; the browser block's search_cls_max must be under 0.1, and the raw runs must read 0 after the first.) |
| 85 | The emulator-definition pack is published for the update channel | Each tag builds, signs and verifies community-defs.tar.gz and uploads it with its signature and index.json to the emulator-defs release | Manual (procedure: after a tagged CI run with the release key, confirm the emulator-defs release lists community-defs.tar.gz, community-defs.tar.gz.sig and index.json, then run python3 -B scripts/verify_release.py --key openbox-release.pub community-defs.tar.gz community-defs.tar.gz.sig on the downloaded files.) |
| 86 | A group's fix is applied to many games and the launch check must stay correct | Only the group's games are checked again; the merged report equals a full audit of the library, and a stale report is refused | Tested (tests/test_launch_audit.py) |

## 1.17.0

Added after the 1.16.1 section so existing row numbers stay stable. Every `Tested` row names a test that exists (ADR 0064, rule 1); `Manual` rows carry the procedure.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 87 | GOG registry entry has no usable `.info` file | The registry `exe` is used when it stays inside the install dir; otherwise the game is skipped with an error naming the folder, not a crash | Tested (tests/test_parity_storefront_windows.py) |
| 88 | A GOG `.info` play task points outside its install dir | The escaping executable is refused; the game is not imported with a launch command that could run another file | Tested (tests/test_parity_storefront_windows.py) |
| 89 | An EA app game is installed in a custom directory | Discovery reads the install root and the game's `__Installer/installerdata.xml`; valid content ids build the launch URI, malformed manifests are skipped | Tested (tests/test_parity_storefront_windows.py) |
| 90 | A Ubisoft Connect install key has no usable `InstallDir` | The entry is skipped and an error names the install id; no half-made game record is added | Tested (tests/test_parity_storefront_windows.py) |
| 91 | A native storefront game is imported twice | Provider id matches the existing record, so the second import adds nothing | Tested (tests/test_windows_storefront_http.py) |
| 92 | The EA app or Ubisoft Connect URI handler is missing or broken | The game record remains; launch fails with the session error naming the game and its launcher. Reinstall the launcher to restore launch. | Manual (procedure: uninstall the EA app or Ubisoft Connect, launch one of its imported games, confirm the session error names the game; reinstall the launcher and confirm the same game launches.) |
| 93 | A deck name, filter, player count, minute budget, or deck count is out of bounds | The save is refused with a 400; stored decks are repaired on load and capped at 20 | Tested (tests/test_parity_party.py, tests/test_handlers_party.py) |
| 94 | The same deck seed is used on two devices with the same library | Both draws produce the same order; a changed library changes the draw | Tested (tests/test_parity_party.py) |
| 95 | A price is negative, not an integer, or cleared | Bad values are refused; clearing removes both the amount and its currency on the single-game and bulk paths | Tested (tests/test_catalog.py, tests/test_gapfill_rest.py) |
| 96 | Cost per hour for an unpriced game or under a minute played | No rate is shown or ranked; totals still count the priced game | Tested (tests/test_insights.py) |
| 97 | A non-English locale receives a cloud-sync, launch-check, definition, or webhook notice | The title and body come from the locale files; a missing key falls back to the stored English text | Tested (tests/test_notifications.py) |

## 1.18.0

Added after the 1.17.0 section so existing row numbers stay stable.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 98 | A plugin declares a capability without its required hook, or an unknown hook | Manifest validation refuses the package and the Plugins manager shows why; valid capability manifests still load | Tested (tests/test_plugin_capabilities.py) |
| 99 | A storefront plugin is disabled or removed while its imported games exist | The plugin's persisted games are deleted from state, the next library projection omits them, and `game_removed` fires | Tested (tests/test_parity_admin_http.py) |
| 100 | A metadata plugin returns fields and media URLs | Applying the candidate records `metadata_provider` and queues each validated URL without re-querying the plugin | Tested (tests/test_plugin_providers.py) |
| 101 | A stats plugin is disabled after writing a cache | Its cached playtime and achievements stop merging into the library immediately; malformed caches are ignored | Tested (tests/test_plugin_stats.py) |
| 102 | A plugin emulator section is asked for a scope it did not declare | The save is refused with a 400; declared platform scopes still save | Tested (tests/test_parity_admin_http.py) |
| 103 | A startup video setting points at a missing file or outside the data media folder | The route refuses it with a 404 and Big Box starts without a video | Tested (tests/test_parity_admin_http.py) |
| 104 | A backup contains plugin credentials or password-format plugin settings | The credential store is excluded and password settings are blanked; plugin data bags are preserved | Tested (tests/test_backup_redaction.py) |
| 105 | The POSIX CI suite runner gives a suite a single chance | A wall-clock budget that only misses on a loaded shared runner failed the Linux, macOS and release jobs while every developer machine stayed green, because `run_all_tests.sh` ran each suite exactly once while the local gate allows three attempts | Tested (`run_all_tests.sh` retries a failing suite up to 3 attempts, prints the first attempt's output when a later attempt passes, and still fails a suite that fails every attempt; proven by `tests/test_ci_gates.py`) |
| 106 | The macOS update applier reaches its relaunch step off macOS | `OPENBOX_DISABLE_EXTERNAL_OPEN`, the same guard `pkg/platform_compat.py` honors, makes the embedded shell script skip `/usr/bin/open`: the bundle swap still completes and the script returns, instead of wedging on a desktop browser the gate has to wait out | Tested (`tests/test_updates.py` asserts the guard is checked before the open, runs the real applier with it set under a timeout, and still verifies the bundle swap and the `.previous` backup) |

## 1.19.1

Added after the 1.18.0 section so existing row numbers stay stable. Every row
describes a platform mismatch the capability contract ([ADR 0086](adr/0086-platform-capability-contract.md))
now prevents.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 107 | A stored gamescope preset reaches a Windows or macOS launch | The preset is ignored: the launch argv is unchanged, no window marking is scheduled, and the preset control is hidden because `features.gamescope` is false | Tested (`tests/test_launch_gamescope_preset.py` `test_non_linux_never_wraps` and `test_non_linux_does_not_mark_the_window`; `tests/test_capability_contract.py` proves the control carries the feature) |
| 108 | The battery indicator asks a Windows laptop or desktop | `/api/native/battery` reads `GetSystemPowerStatus`: a desktop reports absent, a laptop reports percentage and charging state, an unknown percentage reports 0 rather than 255, and the reading is cached for 30 seconds | Tested (`tests/test_platform_compat.py` `test_the_windows_battery_parser_covers_desktop_and_laptop_states` and `test_the_windows_battery_reading_is_cached`; `tests/test_native_handlers.py` covers the route) |
| 109 | A stale client calls the Flatpak folder grant on Windows or macOS | The request is refused with a 400 naming Linux before any subprocess runs, and the grant buttons are hidden because `features.flatpak` is false | Tested (`tests/test_parity_flatpak_grant.py` `test_off_linux_the_grant_is_refused_before_any_subprocess`; `tests/test_platform_route_guards.py` `test_the_flatpak_grant_is_refused_off_linux`) |
| 110 | The Steam or Heroic catalog has no URL opener on the host | The catalog route returns a 400 with a clear message instead of assuming `xdg-open`; installed entries keep their real launch command, and uninstalled entries use the platform opener | Tested (`tests/test_parity_storefront.py` `test_a_host_without_a_url_opener_says_so`, `test_uninstalled_steam_entries_use_the_platform_opener`, and `test_uninstalled_heroic_entries_use_the_platform_opener`) |
| 111 | The Wine/Proton manager is reached on a host without Wine | `/api/wine/*` answers with empty lists and `available: false` and never scans the disk, and the manager panel is hidden because `features.wine` is false | Tested (`tests/test_wine_faugus.py` `test_off_linux_scanners_report_nothing`; `tests/test_platform_route_guards.py` `test_wine_routes_report_unavailable_off_linux`) |
| 112 | A Windows client offers Lutris or a Linux client offers GOG/EA/Ubisoft | The storefront list is filtered by capability, the native Windows import POST refuses other hosts with a 400 and never runs its discoverer, and Epic on Windows still imports through Heroic | Tested (`tests/test_windows_storefront_http.py` `test_the_storefront_list_follows_the_capability_contract` and `test_the_windows_importers_refuse_other_hosts`; `tests/test_capability_contract.py` pins the Windows feature set) |

## Unreleased

Added after the 1.19.1 section so existing row numbers stay stable. These rows back the Unreleased changelog entry under ADR 0064.

| # | Scenario | Expected behavior | Status |
|---|---|---|---|
| 113 | Settings > About offers the Buy me a coffee button | The pane shows a one-line support note and a Buy me a coffee button under the version facts. Clicking the button opens https://www.buymeacoffee.com/haydenopenbox through the same external-link path the other app links use, so a native window hands the page to the system browser | Manual (procedure: run `OPENBOX_DATA_DIR=$(mktemp -d) python3 -B web_app.py --no-browser`, open the printed URL, then choose Settings and About. The note and button must show. Click the button and confirm the donation page opens. Repeat in the native window with `./openbox-native.sh`; the click must open the system browser, not a page inside OpenBox.) |
| 114 | The Buy me a coffee funding link or the AppStream donation URL is removed | GitHub lists the Buy me a coffee funding link from `.github/FUNDING.yml`, and `openbox.metainfo.xml` carries a `donation` URL, which the desktop-appstream CI job validates with appstreamcli | Manual (procedure: run `gh api graphql -f query='{ repository(owner: "vindeckyy", name: "OpenBoxGL") { fundingLinks { platform url } } }'`; the output must include BUY_ME_A_COFFEE. Run `grep 'type="donation"' openbox.metainfo.xml`; it must match one line.) |
| 115 | v6 library.json migrates to v7 keeping unknown fields, game paths, and game IDs | Migration writes the envelope (format, library_id, created_with, path_mode, library_root), keeps every unknown field, and moves active sessions to sessions.json | Tested (tests/test_state_v7.py) |
| 116 | The v6 copy kept before migration is byte-identical to the original file | `library.v6.backup.json` matches the pre-migration bytes; a differing older copy is renamed, never overwritten | Tested (tests/test_state_v7.py, tests/test_cli_library.py) |
| 117 | Migration crashes after the v6 copy but before the primary is replaced | The original library is untouched; the marker remains and the next start retries the migration | Tested (tests/test_state_migration_rollback.py) |
| 118 | The v6 copy cannot be written (disk full) | The migration stops with STATE_BACKUP_MISSING; the primary is not changed and no partial copy is left | Tested (tests/test_state_migration_rollback.py) |
| 119 | The migration report cannot be written after the v7 primary is in place | Startup still succeeds; the next start writes a reconstructed report from the marker | Tested (tests/test_state_migration_rollback.py) |
| 120 | Rollback after migration with post-migration edits | The exact v6 bytes are restored, the replaced v7 file is kept as library.v7.kept-<stamp>.json, and the discarded game edits are listed | Tested (tests/test_state_migration_rollback.py, tests/test_cli_library.py) |
| 121 | Rollback while OpenBox is answering on its port, or sessions are recorded | Rollback refuses and changes no file | Tested (tests/test_state_migration_rollback.py) |
| 122 | A library written by a newer schema (v8) is opened by this build | STATE_FUTURE_VERSION; the file is not rewritten and startup does not auto-recover from the backup | Tested (tests/test_state_v7.py) |
| 123 | Library integrity check on a damaged library | Exit 0 healthy, 1 warnings (missing or outside-root paths), 2 errors (duplicate ids, escape tokens, future schema); nothing is modified | Tested (tests/test_state_migration_rollback.py, tests/test_portable_library.py) |
| 124 | Portable token escape attempt such as `@library/../../etc/passwd` | The token is rejected with PATH_TOKEN_ESCAPE and never resolves outside the root | Tested (tests/test_portable_paths.py, tests/test_portable_library.py) |
| 125 | Windows token that names a drive or an alternate data stream | Rejected with PATH_TOKEN_ESCAPE; drive-relative and colon segments never resolve | Tested (tests/test_portable_paths.py) |
| 126 | Portable library with no root set, or a token with a NUL byte | LIBRARY_ROOT_MISSING or PATH_TOKEN_INVALID is reported; no path is guessed | Tested (tests/test_portable_paths.py) |
| 127 | Library folder moved to another drive | Tokens resolve to the new folder without rewriting games; paths outside the old root stay absolute and are reported | Tested (tests/test_portable_library.py) |
| 128 | Move apply after the library changed since the preview | MOVE_PLAN_STALE (409); the library file is byte-identical to before the attempt | Tested (tests/test_portable_library.py) |
| 129 | API v1 requested while OPENBOX_ENABLE_V1_API is unset | 410 V1_API_DISABLED with a pointer to /api/v2; with the variable set, the frozen aliases answer and carry Deprecation and Sunset | Tested (tests/test_api_v2_contract.py) |
| 130 | Native host sends a different major API version | 409 HOST_API_UNSUPPORTED; the window shows a version message instead of parsing an unknown body | Tested (tests/test_api_v2_contract.py) |
| 131 | An error response on the v2 surface | Body is a problem document (type, title, status, detail, code, retryable, request_id) and still carries the legacy error field | Tested (tests/test_api_v2_contract.py) |
| 132 | A flat `parity_*` import is written in source or resolved at runtime | The gate fails the change and the flat name does not resolve at runtime | Tested (tests/test_flat_imports.py) |
| 133 | Backup member altered after the manifest was written | Restore fails with a SHA-256 error before any file is written; the existing file is kept | Tested (tests/test_backup_format_v2.py) |
| 134 | Backup manifest declares a format newer than this build | BACKUP_FORMAT_UNSUPPORTED; nothing is restored | Tested (tests/test_backup_format_v2.py) |
| 135 | Archive member that the manifest does not list | Restore refuses the archive before any write | Tested (tests/test_backup_format_v2.py) |
| 136 | 1.19-compatible backup export restored by a 1.19 build | The library is written in the v6 layout (schema 6, no envelope), with sessions kept | Tested (tests/test_backup_format_v2.py) |
| 137 | macOS notarization secrets are unavailable at release time | The release is built ad-hoc and the notes state the bundle is not notarized; no stable tag claims notarization without spctl evidence | Manual (procedure: run `scripts/build_macos_app.sh` with no notary secrets set, run `spctl --assess --type execute` on the app, and confirm the release notes say "not notarized") |
| 138 | Windows ARM64 host runs on an x64-only build environment | The ARM64 artifact is deferred to 2.0.1 and the installer keeps refusing ARM64 with a clear message | Manual (procedure: on a Windows 11 ARM64 device run `scripts/install.ps1`; it must print the refusal and the x86_64 emulation path, and must not start a window) |
| 139 | Library of 50,000 games opened and written | Measured and reported as exploratory; not a release blocker (ADR 0098) | Manual (procedure: `python3 -B scripts/perf_bench.py --sizes 50000 --no-gate`, record date, machine, and commit in docs/development/PERF.md) |
| 140 | Portable library opened on Windows and macOS after a move | The same tokens resolve on each platform with its own separators; NFC and NFD spellings match | Manual (procedure: move a library with 20 games and one NFD folder name between two machines, run `--check-library`, expect exit 0 and no path problems) |
| 141 | Library check finds a missing, unreadable, or outside-folder game path | The integrity report counts each problem in its own category and names examples; nothing is written | Tested (tests/test_library_integrity.py) |
| 142 | A downloaded media file is changed, deleted, or added behind the app's back | The media manifest reports the file as changed, missing, or untracked; a repair refreshes the manifest | Tested (tests/test_media_manifest.py) |
| 143 | Backup restored with only some items selected | Every member is hash-checked first, and the report says which members were restored, skipped, or verified | Tested (tests/test_backup_format_v2.py) |
| 144 | A 1.19-compatible export is restored by a 1.19 build | The library is written in the 1.19 layout and restores on the older build | Tested (tests/test_backup_fixtures.py) |
| 145 | A theme is missing tokens, or declares a token the app does not know | The compatibility report names the fallback tokens and the unknown token; the theme still renders | Tested (tests/test_theme_compat.py) |
| 146 | The compatibility report is opened twice on the same data | Both reports are equal, because the report carries no timestamps | Tested (tests/test_compat_report.py) |
| 147 | A native window from another major API version talks to the server | The server refuses it with `HOST_API_UNSUPPORTED` before the route runs | Tested (tests/test_api_v2_contract.py) |
| 148 | An ARM64 Windows host asks for an update before the ARM64 artifact is published | The updater asks for `OpenBox-arm64-windows.zip`; the installer refuses ARM64 with the published-builds message | Tested (tests/test_windows_arm64.py) |
| 149 | A failure with a known code reaches the UI | The UI shows the localized explanation for the code instead of the raw server text | Tested (tests/test_api_problem_details.py) |
| 150 | A game carries extra-app paths inside the library folder | The extra paths are stored as tokens and are rebased by a move, like the game path | Tested (tests/test_portable_library.py) |
| 151 | Settings is driven with the keyboard only | Focus enters the dialog, Tab reaches every new control in document order, each control has an accessible name, and Escape leaves the dialog | Manual (procedure: `scripts/ui_smoke_a11y.cjs` drives Chrome with the keyboard and checks focus order, names, and the reduced-motion tokens) |
| 152 | A plugin declares an API version this build does not serve | The plugin is refused and the message names the version it asked for and the versions this build serves | Tested (tests/test_plugin_api_v2.py) |
| 153 | A v1 plugin runs under a build that also serves plugin API v2 | The v1 runner and the v1 payload are used and the result shape is unchanged | Tested (tests/test_plugin_api_v2.py, tests/test_plugins.py) |
| 154 | A v2 plugin returns more output than the cap allows | The runner stops with `PLUGIN_OUTPUT_TOO_LARGE` instead of flooding the host | Tested (tests/test_plugin_api_v2.py) |
| 155 | A plugin asks for a host service version the host does not serve | The request is refused; every service that is served is read-only and bounded | Tested (tests/test_plugin_services.py) |
| 156 | A client reads a typed operation's schema in the OpenAPI document | The schema matches the payload the server actually returns; a drifted schema fails the gate | Tested (tests/test_openapi.py) |
| 157 | The search projection is stale after a write | The read falls back to the JSON path and reports `source: "json"`; the rebuild lands in the background and no request waits for it | Tested (tests/test_search_http.py, tests/test_sqlite_readmodel.py) |
| 158 | The read-model database is corrupt | The database is discarded and rebuilt from the canonical library; the library itself is never lost | Tested (tests/test_sqlite_readmodel.py) |
| 159 | The library filter chips count a large library | The counts are computed by the search worker, the chips keep the previous counts until the reply arrives, and a reply for an older generation is ignored | Tested (tests/test_js_pure.py) |
| 160 | 2.0 starts on a library that still needs the upgrade | The migration gate covers the app, names the old and new schema, the game count, and where the copy of the old library is, and hands the library over only when the user opens it | Tested (tests/test_upgrade_flow.py, scripts/ui_smoke_migration.cjs) |
| 161 | Something in the install needs the user's attention before the upgrade | The gate lists it: a theme with a token the app does not know, a plugin with an API version this build does not serve, a missing sync folder, an archive with no format, a library over the formal scale, or a custom data directory | Tested (tests/test_upgrade_flow.py) |
| 162 | The user wants a backup before opening the upgraded library | The gate writes one and names the archive; the copy of the old library stays where it was | Tested (scripts/ui_smoke_migration.cjs) |
