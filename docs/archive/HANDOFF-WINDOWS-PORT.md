# Handoff: Porting OpenBoxGL to Windows

Status: **complete — archived.** The port shipped under [ADR 0048](../adr/0048-windows-port.md);
this document is kept for traceability, and the "in progress"/"not started" sections below
describe the state at the time they were written, not the current tree. Windows coverage is
the `windows-latest` job in `.github/workflows/ci.yml`, and the durable design record is the ADR.

---

## 1. Mission and scope (decided with the user)

The user asked to "perfectly port OpenBoxGL to Windows". Three scope decisions were made
explicitly before work began:

1. **Native window: build a WebView2 host.** Not browser-only. A Windows `native_host`
   equivalent using WebView2 is required. (The Linux one is a WebKitGTK/GTK3 C program
   that cannot compile on Windows.)
2. **Packaging: "everything".** Windows launcher scripts, data dir under `%LOCALAPPDATA%`,
   installer, Start Menu integration, `openbox://` protocol registration, and updater flow.
3. **Tests/CI: platform-guard + Windows CI.** Add `IS_WINDOWS` guards and skip helpers to
   Linux-only tests, make the core suite pass on Windows, and add a `windows-latest` CI job.

The repo's own rules (`AGENTS.md`) still apply: runtime stays dependency-free (stdlib only,
including the Windows ctypes bindings), v1 route surface is frozen, coverage floors ratchet
up only, and new runtime modules must be listed in `runtime_modules.txt`.

---

## 2. Environment facts (verified on this machine)

| Item | Value |
| --- | --- |
| OS | Windows (win32), PowerShell 5.1 is the shell |
| Python | 3.12.10 (`python` and `py` both work; `python3` is a Microsoft Store shim) |
| Repo root | `C:\Users\theha\OneDrive\Desktop\Projects\OpenBoxGL` |
| Git branch | `master`, no commits made yet by this work |
| MSVC / `cl.exe` | **NOT INSTALLED.** No `gcc`, `clang`, `make`, `mingw32-make` either. |
| `cmake` | Installed via WinGet (4.4.3), path under `AppData\Local\Microsoft\WinGet\Packages\...` |
| `dotnet` | Installed (`C:\Program Files\dotnet\dotnet.exe`) |
| WebView2 runtime | **Installed**, `C:\Program Files (x86)\Microsoft\EdgeWebView\Application\153.0.4234.48` |
| `winget` | Available (v1.29.290) |
| `node` / `npm` | **NOT INSTALLED** (frontend lint gate cannot run locally) |
| `7z`, `ffmpeg`, `rg` (ripgrep) | **NOT INSTALLED** |
| `curl.exe` | Present in `C:\WINDOWS\system32` |
| Temp dir for scratch work | `C:\Users\theha\AppData\Local\Temp\opencode` (pre-approved) |

Because there is no compiler, **the WebView2 host has not been written or built yet.**
See §8.

---

## 3. Verification commands

There is no `make`, no bash, and no ripgrep. Use these instead:

```powershell
# Run the whole suite with per-file pass/fail reporting (new helper, see §6)
$env:OPENBOX_DATA_DIR = "C:\Users\theha\AppData\Local\Temp\opencode\obx-smoke"
python -B scripts/run_windows_tests.py

# Run one file
python -B tests/test_sessions.py

# Run a subset
python -B scripts/run_windows_tests.py test_moments.py test_sessions.py

# Import smoke test
python -B -c "import web_app; print('web_app OK')"

# v1 contract gate (works on Windows already)
python -B scripts/check_v1_contract.py

# Full gate (will FAIL on Windows today — see §9)
python -B scripts/check_tests.py
```

Per-file results are written to
`C:\Users\theha\AppData\Local\Temp\opencode\windows-test-results.json`
(with `stderr_tail`/`stdout_tail` for each failure).

---

## 4. Architecture: `pkg/platform_compat.py` (the core of the port)

**New file, currently untracked.** This is the single seam for every platform difference.
All runtime modules import from it instead of using POSIX-only primitives directly.
It is stdlib-only. Windows code paths are guarded by `if IS_WINDOWS:` and marked
`# pragma: no cover` so the Linux coverage gate does not require them; the Windows CI job
executes them.

### Public API (keep names stable; many modules import these)

```python
IS_WINDOWS, IS_POSIX, IS_MACOS
WINDOWS_EXECUTABLE_SUFFIXES
DEFAULT_LOCK_TIMEOUT

default_data_dir() -> Path          # %LOCALAPPDATA%\openbox-game-launcher on Windows
env_file_roots() -> list[Path]      # %USERPROFILE%, %APPDATA%, %LOCALAPPDATA% on Windows
home_dir() -> Path | None           # never raises when HOME is unset

lock_handle(handle, *, exclusive=True, timeout=30.0, poll=0.05)  # context manager
file_lock(path, *, exclusive=True, timeout=30.0)                 # context manager

process_alive(pid) -> bool
terminate_process(pid, *, force=False) -> bool
terminate_process_tree(pid, *, force=False, pgid=None) -> bool
suspend_process_tree(pid, *, pgid=None) -> bool
resume_process_tree(pid, *, pgid=None) -> bool
process_group_id(pid) -> int        # PID itself on Windows
launch_kwargs(*, detached=False, new_group=True, no_window=False) -> dict

process_start_token(pid) -> str | None   # creation FILETIME on Windows
process_command_line(pid) -> str         # image path on Windows
process_name(pid) -> str
process_cwd(pid) -> str                  # "" on Windows
child_pids(pid) -> list[int]
find_pids_by_name(pattern) -> list[int]
find_pids_in_folder(folder) -> list[int]

split_command(command) -> list[str]      # platform quoting rules
join_command(parts) -> str
is_executable(path) -> bool              # suffix-based on Windows
executable_paths(commands) -> list[str]

open_path(target) -> None                # os.startfile on Windows
reveal_path(target) -> None              # explorer /select,
open_url(url) -> None
browser_application_commands() -> list[str]
resolve_browser_application() -> str | None
platform_label() -> str

file_is_private(info) -> bool
is_group_or_world_writable(info) -> bool
owned_by_current_user(info) -> bool

steam_install_roots() -> list[Path]      # registry + Program Files on Windows
epic_manifest_paths() -> list[Path]

windows_process_table() -> list[dict]    # [] on non-Windows; used by parity_gamescope
_split_windows_command(command) -> list[str]  # MSVCRT rules; testable from Linux
```

### Key Windows implementations

- **Locking**: `msvcrt.locking(fd, LK_NBLCK, 1)` with a retry loop and timeout, unlocked
  with `LK_UNLCK`. POSIX keeps `fcntl.flock`.
- **Liveness**: `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(handle, 0) == WAIT_TIMEOUT`.
  **Never `os.kill(pid, 0)`** — on Windows CPython that calls `TerminateProcess`.
- **Termination**: graceful `taskkill /PID x /T` first, then `taskkill /PID x /T /F`,
  then `TerminateProcess`. All exceptions swallowed (probing must never raise).
- **Suspend/resume**: `ntdll.NtSuspendProcess` / `NtResumeProcess` via ctypes.
- **Identity**: `GetProcessTimes` creation FILETIME as the start token;
  `QueryFullProcessImageNameW` for the command fingerprint.
- **Process table**: `CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS)` for `child_pids`,
  `find_pids_by_name`, `find_pids_in_folder`.
- **Steam**: registry `HKCU/HKLM\Software\Valve\Steam` `SteamPath` + `ProgramFiles(x86)\Steam`.

---

## 5. Runtime changes already made (by area)

All of these are uncommitted. `git diff --name-only` lists ~100 files; the runtime subset is:

### Import blockers removed

- `state_store.py` — dropped `import fcntl`; `_file_lock` now uses `lock_handle`.
- `cloud_sync.py` — dropped `import fcntl`; `_sync_lock` uses `file_lock`.
- `pkg/parity/parity_household.py` — dropped `import fcntl`; `_household_transport_lock`
  uses `lock_handle` and maps `TimeoutError` to `SyncFolderError`.
- `pkg/parity/parity_media.py` — dropped `import fcntl`; `_queue_lock` uses `lock_handle`.
- `env_config.py` — replaced `os.geteuid()`/`st_mode & 0o077` owner checks with
  `file_is_private`; `discover_env_files` uses `env_file_roots()`.

### `/proc` and process supervision

- `pkg/state/launch.py`:
  - `_read_proc_start_time` → `process_start_token`; `_read_proc_cmdline` → `process_command_line`.
  - `_verify_process_identity` uses `process_alive` (not `os.kill(pid, 0)`).
  - `_terminate_owned_process` → `terminate_process_tree(pid, force=False, pgid=...)`.
  - `_make_start_mutator` uses `process_group_id`.
  - `control_game_session` uses `suspend_process_tree` / `resume_process_tree` /
    `terminate_process_tree` and raises `ValueError` when they return False.
  - `_start_launch_command` uses `split_command` and `is_executable`.
  - `subprocess.Popen(..., **launch_kwargs())` instead of `start_new_session=True`.
  - Removed `import shlex` and `import signal`.
- `pkg/parity/parity_tracking.py` — all `/proc` helpers now delegate to `platform_compat`
  (`_proc_name`, `_proc_cmdline`, `_proc_cwd`, `_alive`, `find_pids_by_name`,
  `find_pids_in_folder`). `_alive` stays monkeypatchable (tests patch it).
- `web_app.py`:
  - Shutdown path uses `process_alive` + `terminate_process_tree`.
  - `_sanitize_error_message` now strips Windows paths (`C:\...`, UNC) as well as POSIX.
  - Imports `process_alive`, `terminate_process_tree` from `platform_compat`.
- `pkg/parity/parity_gamescope.py`:
  - `_child_pids` uses `/proc` on POSIX and `_windows_child_pids` (via
    `windows_process_table`) on Windows.
  - `resolve_kiosk_browser` uses `browser_application_commands()` /
    `resolve_browser_application()` when no `which` is injected; skips Flatpak on Windows.
  - `open_ui` uses `launch_kwargs()`; on Windows falls through to `open_path(target)`
    before the `xdg-open` branch.

### Command quoting and path separators

- `openbox.py` — `default_data_dir()` for the data root; `join_command`/`split_command`;
  Wine profile only registered on non-Windows; `.bat/.cmd/.lnk` added to `EXTENSIONS`;
  self-test branches on `os.name`.
- `pkg/parity/launch_tokens.py` — `_build_context` uses `_path_parts()` which keeps the
  **separator flavor of the input path** (POSIX paths stay `/`-joined even on Windows;
  this is what makes `/roms/nes` tokens stable cross-platform). `build_launch_args` uses
  `split_command`.
- `pkg/state/commands.py` — `split_command` + `launch_kwargs`.
- `pkg/state/imports.py` — `join_command`.
- `handlers/sessions.py` — `split_command` + `launch_kwargs`; documents use `open_path`
  (removed the `shutil.which("xdg-open")` requirement; `shutil` import removed as unused).
- `handlers/extensions.py` — `open_themes_folder` uses `open_path`; `subprocess` import
  removed as unused.
- `emulators.py` — `join_command`, `launch_kwargs`; Flatpak install/update paths remain
  Linux-only by nature.
- `arcade.py` — `join_command`/`split_command`.
- `importers.py` — biggest storefront change:
  - `steam_roots()` uses `steam_install_roots()` when no explicit `home`.
  - `steam_command()` prefers `steam.exe` under detected Steam roots on Windows, then
    `flatpak`, then `xdg-open`/`explorer.exe` URL fallback.
  - New `_url_opener()` / `url_open_command()` helpers.
  - `import_heroic()` keeps the Linux `xdg-open heroic://` path when an explicit `home`
    is passed (tests rely on it) and uses `_import_heroic_windows()` when `home is None`
    on Windows (launches the game's own `.exe`).
  - New `import_epic()` reading Epic manifests from `%PROGRAMDATA%\Epic\...\Manifests`.
  - `join_command` for Lutris.
  - `import shlex` is now **unused** in this file — remove it (ruff `F401` will catch it).
- `pkg/parity/parity_emulator_defs.py`:
  - New `native_exe_windows` YAML field (not yet added to any YAML — see §8).
  - `detect_adapter_prefix` prefers `native_exe_windows` and skips Flatpak on Windows.
  - `join_command` at all profile-building sites.
  - `.sh` fallback resolves `bash`/`sh` via `shutil.which` and raises a clear
    `FileNotFoundError` on Windows when neither exists.
- `pkg/parity/parity_gameyfin.py` — `owned_by_current_user` + `is_group_or_world_writable`
  instead of `os.geteuid()`/`0o022`; filesystem-root guard uses `root.anchor`.
- `handlers/moments.py` — `_resume_link` stores the snapshot reference as
  `snapshot.relative_to(state_dir).as_posix()` so it stays `moments/...` on Windows.
  `_snapshot_path` already normalizes `\` → `/`.
- `pkg/state/cache.py` — `_media_set_contains` normalizes with `posixpath.normpath` for
  `/`-prefixed paths and `os.path.normpath` for host-absolute paths; added
  `import posixpath`.
- `saves.py` — new `_discover_save_paths_windows()` (`Documents`, `%APPDATA%`,
  `%LOCALAPPDATA%` roots for PCSX2/PPSSPP/RPCS3/Dolphin/RetroArch/Cemu/Steam userdata);
  `discover_save_paths` dispatches on `IS_WINDOWS` only when `home is None`.
- `archives.py` — replaced `selectors.DefaultSelector` pipe polling (unsupported on
  Windows pipes) with a reader thread + deadline + `finished` Event.
- `crash_report.py` — `_WINDOWS_HOME_PATH_RE` redaction; `install_channel()` returns
  `"windows"`.
- `pkg/platform_compat.py` — new module (see §4).

### Route surface (new, additive)

- New `POST /api/import/epic` (handler `handlers/imports.py::import_epic_games`) plus its
  v1 alias. Registered in `routes.py` (`POST_TABLE`, `V1_ALIASED_PREFIXES`), frozen in
  `v1_contracts.json`, and referenced in `static/util.js` (`import_epic`).
- Tests updated for the new counts: `test_routes_registry.py` (178 GET / 209 POST /
  61 aliases / 320 registry rows / 166 base POST), `test_v1_aliases.py` (reads `util.js`).
- **UI wiring is NOT done:** `static/imports.js` has no `importEpic()` and no button.
  Add it if the Epic importer should be user-visible.

### Encoding (PEP 597)

Windows defaults to `cp1252` for text I/O, which broke ~50 test files and several runtime
reads. A codemod added explicit `encoding="utf-8"` to zero-argument `read_text()` and
single-argument `write_text()` calls across the repo (~300 call sites, ~100 files). This is
why the diff is large. **Runtime `open()` text calls without encoding were not all audited**
— see §9.

### Test-only fixes already landed

`test_archives.py`, `test_backend_hardening.py`, `test_crash_report.py`,
`test_emulator_health.py`, `test_emulators.py`, `test_env_config.py`,
`test_error_handling.py`, `test_importers.py`, `test_launch_phases.py`,
`test_launch_tokens.py`, `test_logging.py`, `test_media_paths.py`, `test_moments.py`,
`test_operations.py`, `test_parity_esde_import.py`, `test_parity_gameyfin.py`,
`test_parity_household.py`, `test_perf_cache.py`, `test_perf_writes.py`, `test_plugins.py`,
`test_retroachievements.py`, `test_routes_registry.py`, `test_saves.py`,
`test_session_persistence.py`, `test_sessions.py`, `test_state_v4.py`, `test_v1_aliases.py`,
`test_wine_faugus.py`.

Patterns used: `if os.name != "nt"` around POSIX mode-bit assertions; `skipTest` for
`os.mkfifo`/symlink/invalid-UTF-8-name cases; Python-script fixtures instead of `#!/bin/sh`
fixtures; patching `pkg.state.launch.*` / `pkg.platform_compat.*` helpers instead of
`os.killpg`/`os.getpgid`; `sys.executable` instead of `/bin/true`; explicit UTF-8 encoding.

---

## 6. New dev helper: `scripts/run_windows_tests.py`

Untracked. Runs every `tests/test_*.py` in a subprocess with a 120 s timeout, prints
`pass`/`fail`/`timeout` per file, and writes JSON results (including last 12 stderr lines)
to `%TEMP%\opencode\windows-test-results.json`. It sets `PYTHONPATH` to the repo root and
`PYTHONIOENCODING=utf-8`. Accepts an optional list of file names to run a subset.
This is a dev tool — do not add it to `runtime_modules.txt`.

---

## 7. Current test status: 123/134 passing

Remaining 11 failures, with diagnosis:

### 7.1 `test_parity_resume.py` (7 failures, 11 errors)

Root cause: `parity_resume.inject_resume_args` raises
`ValueError("No state-capable adapter is configured for this game.")` and
`collect_resume_state` returns `None`. The tests build adapters inline; the failure is
likely that `detect_adapter_prefix` / `build_adapter_argv` no longer resolve because the
tests inject `which=lambda name: "/usr/bin/" + name` while the new Windows-aware prefix
resolution changes behavior, or because `_path_parts`/`join_command` changed an expected
string. Inspect the first failing test:

```
tests/test_parity_resume.py::StatusAndStaleTests::test_status_and_resume_reject_traversal_and_symlink_files
```

Run it and read the traceback. Compare against `pkg/parity/parity_resume.py`
lines ~260-330 (`_safe_state_path`, `inject_resume_args`) and the adapter fixtures in the
test file. This is the highest-value remaining fix because `test_resume_http.py`
(5 failures) depends on the same code path.

### 7.2 `test_resume_http.py` (5 failures)

Same root cause as 7.1. Fix 7.1 first.

### 7.3 `test_parity_gamescope.py` (3 failures, 4 errors)

`TypeError: fake_popen() got an unexpected keyword argument 'creationflags'`. The test's
`fake_popen(args, start_new_session=False, env=None)` stubs predate `launch_kwargs()`.
Two options:
- Update the six `fake_popen` stubs in the test to accept `**kwargs`.
- Or have `open_ui` pass `launch_kwargs()` only when `popen` is not injected.

The second is cleaner and keeps the injected-opener contract unchanged; but check
`test_parity_gamescope.py` lines ~95-220 before deciding. Also
`test_open_ui_desktop_uses_xdg_open_with_clean_env` expects `xdg-open` to be used on
Windows — that test should be guarded with `os.name != "nt"` or given a Windows branch
(Windows intentionally prefers `os.startfile`).

### 7.4 `test_launch_dedupe_http.py` (1 failure, 3 errors)

Fixtures still use `['/bin/true']` and `/tmp` (lines ~50, 74, 118, 134, 228). Replace with
`[sys.executable]` and a real temp dir. Also
`test_reorder_uses_stable_id_and_delete_rolls_back` hits `KeyError: 'play_count'` —
check whether the test's fake `_start_launch_command` patch still lines up with the new
`_make_start_mutator` (the `process_group_id` call is now outside the try/except).

### 7.5 `test_launch_doctor.py` (2 failures)

`doctor._flatpak_fs_allowed(...)` returns False on Windows. Flatpak checks are meaningless
on Windows. Guard those two assertions with `os.name != "nt"` (or make
`_flatpak_fs_allowed` return a Windows-appropriate result). Test names:
`test_flatpak_fs_grant_paths`, `test_remaining_branches`.

### 7.6 `test_metadata.py` (1 error)

`PermissionError: [WinError 32] ... 'metadata.db'` during `TemporaryDirectory` cleanup —
the SQLite connection is still open when the temp dir is removed. Find the
`sqlite3.connect` in `metadata.py` and ensure the test closes it (or the module uses a
context manager). Test file line context is in the JSON results.

### 7.7 `test_native_host.py` (7 errors)

Expects `pkg-config` + `gcc` to build `native_host.c`, and uses `socket.AF_UNIX`.
All 7 errors are the compiler/socket being absent. **Skip the whole file on Windows**
(`unittest.skipIf(sys.platform == "win32", ...)`) and replace its coverage with a new
Windows host test once the WebView2 host exists (see §8).

### 7.8 `test_packaging.py` (multiple failures)

Asserts Linux packaging: `openbox.sh`/`openbox-native.sh` executability, `make install`,
`.desktop`/AppStream validation, Flatpak, AppRun shims, `make -o native-host`, and a
Windows-illegal `subprocess` invocation. **Gate the Linux-only test functions on
`os.name != "nt"`** and add Windows equivalents (launcher scripts, installer) when they
land. Also `test_legal_policy` fails on a mojibake literal (`No �?" upgrade required`) —
that is a pre-existing test-file encoding artifact; read that file with explicit UTF-8
and fix the literal.

### 7.9 `test_parity_api.py` (2 errors)

`ValueError: Watch folder does not exist: \tmp` and
`ValueError: Gameyfin install folder is invalid: \tmp\gameyfin`. The tests pass POSIX
paths that Windows normalizes to `\tmp`. Replace the fixture paths with real temp dirs, or
have `handlers/settings.py` `_clean_watch_folders`/`_clean_gameyfin` tolerate
non-existent paths on Windows. Prefer fixing the tests: settings validation should stay
strict.

### 7.10 `test_parity_playnite.py` (2 failures)

- `stat.S_IMODE(archive.stat().st_mode) == 0o600` — wrap in `if os.name != "nt"`.
- `test_launch_command_expanded_variables` expects `/usr/bin` but gets `\usr\bin` for the
  `{EmulatorDir}` token. `_path_parts` keeps the separator flavor of `{path}` but
  `emulator_dir` is substituted raw through `apply_tokens`, which is correct — the test
  fixture should pass a POSIX-style emulator dir and assert it verbatim, or the assertion
  should compare `Path` parts. Decide and document in the test.

### 7.11 `test_parity_setup_preview.py` (1 failure)

`test_helper_edge_paths`: `_adapter_installed(adapter)` returns False. Likely because the
adapter fixture has a Linux `native_exe` and no `native_exe_windows`, and the injected
`which` only resolves Linux names. Add `native_exe_windows` to the fixture or adjust the
injected `which`.

---

## 8. Major work NOT started

### 8.1 WebView2 native host (the largest item)

`native_host.c` (Linux, WebKitGTK/GTK3) stays as-is. Create a Windows counterpart, e.g.
`native_host_win.c` or `native_host/windows/`, implementing the same Python-facing
contract:

- Spawns `python.exe -B web_app.py --no-browser` and waits for `server.port` /
  `server.token` in the data dir.
- Sets `OPENBOX_NATIVE_HOST=1` for the child.
- Reads `native-host-flags` (`tray minimize_to_tray`) and `window-geometry`.
- Serves the JS bridge that `handlers/native.py` and `static/state.js` already expect:
  `window.openboxNative` with `dialog`, `openExternal`, `reveal`, `windowAction`,
  `onGamepad` (see `native_host.c` lines ~1501-1549 and `handlers/native.py`).
- Single instance via a named mutex + named pipe (not `AF_UNIX`); forward
  `focus` / `deeplink <uri>` messages.
- Owns shutdown: POST `/api/shutdown`, then terminate the child and its tree.

Implementation notes:
- Use WebView2 (`ICoreWebView2`) with `CreateCoreWebView2EnvironmentWithOptions`.
  The runtime is already installed at version 153.0.4234.48. The SDK headers/nuget can be
  fetched by the build script; **no compiler is installed on this machine**, so the build
  script must either bootstrap MSVC Build Tools via winget or document the requirement.
  Consider a MinGW-w64 fallback if it can link WebView2's COM interfaces.
- JS bridge: `AddScriptToExecuteOnDocumentCreated` injecting the same
  `window.openboxNative` object; messages arrive via
  `window.chrome.webview.postMessage` → `add_WebMessageReceived`.
- File dialogs: `IFileOpenDialog`; reveal: `explorer /select,`; external URLs:
  `ShellExecuteW`; tray: `Shell_NotifyIcon`; window ops: `ShowWindow`.
- Add a `native-host-win` build target to whatever build script you create, and a
  `scripts/` entry point. Do **not** break the existing `make native-host` path.

### 8.2 Windows launcher scripts

`openbox.sh` / `openbox-native.sh` are bash. Add PowerShell/CMD equivalents, e.g.
`openbox.ps1` + `openbox.cmd` (and `openbox-native.ps1`). Requirements:
- Resolve the repo/install root from the script location (`$PSScriptRoot` / `%~dp0`).
- Launch `python.exe -B web_app.py` (browser mode) or the WebView2 host when present.
- Pass through all CLI flags (`--web`, `--game-mode`, `--no-browser`, `--app-window`,
  `--width/--height/--resolution`, `--backup`, `--restore-backup`, deeplinks).
- Keep the same fallback ladder as the bash scripts (native host → web app).

### 8.3 Installer, updater, protocol registration

- `updates.py` is AppImage-only: `ASSET = f"OpenBox-{arch}.AppImage"`, requires the
  `APPIMAGE` env var, and self-replaces the running file. Add a Windows channel:
  - Detect install channel (`install_channel()` already returns `"windows"`).
  - New asset naming (e.g. `OpenBox-Setup-<arch>.exe` or a portable zip).
  - `install_update` cannot replace a running `.exe`; hand off to the installer or a
    helper process (`msiexec`, Inno `/SILENT`, or a small updater exe).
  - `install_desktop_entry` writes `.desktop` files to XDG dirs — replace with a Start
    Menu `.lnk` (WScript.Shell / IShellLink) on Windows.
- Register `openbox://` under
  `HKCU\Software\Classes\openbox\shell\open\command` with `URL Protocol`.
- Decide packaging format: Inno Setup, MSIX, or a portable zip. Include an embedded
  Python (the runtime is stdlib-only, so the embeddable CPython zip works) or require a
  system Python 3.10+.
- `scripts/install.sh` (bash) needs a PowerShell counterpart for the release flow.

### 8.4 Emulator definitions

`emulator_defs/*.yaml` currently carry only Linux `native_exe` names (e.g.
`dolphin-emu`, `duckstation-qt`, `retroarch`) and Flatpak IDs. `detect_adapter_prefix`
already supports a `native_exe_windows` field, but **no YAML uses it yet.** Add Windows
binary names (e.g. `Dolphin.exe`, `duckstation-qt-x64-ReleaseLTCG.exe`, `retroarch.exe`,
`pcsx2-qt.exe`, `rpcs3.exe`, `cemu.exe`, `eden.exe`, `melonDS.exe`, `ppsspp.exe`,
`mame.exe`, `scummvm.exe`, `xemu.exe`, `xenia_canary.exe`, `Vita3K.exe`) plus Windows
BIOS/save hints where the module reads them (`pkg/parity/parity_import.py`,
`pkg/parity/parity_saves.py` still hardcode `~/.local/share/...`).

### 8.5 CI

Add a `windows-latest` job to `.github/workflows/ci.yml`:
- `actions/setup-python@v5` (3.12), `pip install -r requirements-dev.txt`.
- Run `python scripts/run_windows_tests.py` (or a ported `check_tests.py`).
- A separate job to build the WebView2 host once it exists.
- Keep the existing `ubuntu-latest` jobs unchanged.

### 8.6 Gates that must be ported (currently Linux-only)

`scripts/check_tests.py` will fail on Windows:
- `xvfb-run` detection (harmless), but `VENV/bin/ruff` and `VENV/bin/coverage` are POSIX
  paths → need `VENV/Scripts/ruff.exe` / `coverage.exe`.
- `PYTHONPATH` joined with `":"` → use `os.pathsep`.
- `scripts/check_changed_coverage.py` looks in `.venv-dev/lib*/python*/site-packages` →
  add `.venv-dev/Lib/site-packages`.
- `scripts/check_frontend.py` prefers `node_modules/.bin/eslint` (POSIX shim) → prefer
  `.cmd` variants or `npx.cmd`.
- `scripts/check_tokens.py`, `check_v1_contract.py`, `check_version_sync.py`,
  `check_i18n.py`, `check_runtime_modules.py`, `check_csp.py` are already portable.
- `scripts/validate_flatpak_manifest.py` is Linux-only by design.

---

## 9. Critical outstanding items (do these first)

1. **`pkg/platform_compat.py` is NOT in `runtime_modules.txt`.**
   `scripts/check_runtime_modules.py` requires every root `*.py` and every
   `pkg/**/*.py` to be listed. Add it or the gate fails.
2. **No `tests/test_platform_compat.py` exists.** `scripts/check_tests.py` enforces an 85%
   new-module coverage floor on modules newly added to `runtime_modules.txt`. Write tests
   covering: `split_command`/`join_command`, `is_executable`, `lock_handle`/`file_lock`
   (including timeout), `process_alive`/`terminate_process` (with fakes), `_path_parts`
   behavior, `browser_application_commands`, `file_is_private`,
   `is_group_or_world_writable`, `owned_by_current_user`, `home_dir`,
   `default_data_dir`, `env_file_roots`, and the pure-Python
   `_split_windows_command` (testable on Linux too).
3. **Changed-line coverage floor is 95%** over ~1000 changed runtime lines. The
   Windows-only branches are `# pragma: no cover`, but the portable helpers are not.
   Either cover them from the Linux gate or add pragmas with justification.
4. **Unused import**: `import shlex` in `importers.py` (line 6) — ruff `F401` will fail.
   Check other files for imports left behind by the edits (`handlers/sessions.py` and
   `handlers/extensions.py` were cleaned; `pkg/state/launch.py` had `shlex`/`signal`
   removed).
5. **Audit remaining text-mode `open()` calls without `encoding=`** in the runtime.
   The codemod only handled `read_text()`/`write_text()`. Use
   `C:\Users\theha\AppData\Local\Temp\opencode\scan_encodings.py` as a starting point
   (it lists `open()` call sites; review each — many are binary mode and fine).
6. **Frontend i18n**: the new Epic importer has no locale strings or UI. If you add the
   button, add strings to all five `locales/*.json` files (`scripts/check_i18n.py` gates
   this).
7. **`.pre-commit-config.yaml`** hardcodes `python3 -B -m py_compile` — will not run on
   Windows. Either make it portable or document the workaround.

---

## 10. Design decisions to preserve

- **Runtime stays dependency-free.** Do not add `psutil`, `portalocker`, `pywin32`, etc.
  Everything must be stdlib + ctypes. The AppImage/Flatpak builds must keep working.
- **`pkg/platform_compat.py` is the only platform seam.** Do not scatter `sys.platform`
  checks through the codebase. If a new platform difference appears, add a helper there.
- **POSIX behavior must not regress.** Every Windows branch must be additive; the Linux
  test suite must keep passing unchanged (except for the deliberate `encoding=` and
  `os.name` guard edits).
- **`os.kill(pid, 0)` is banned in the runtime.** Always use `process_alive`.
- **Stored relative references use POSIX separators** (`moments/<id>.state`, v1 routes,
  library paths) so libraries stay portable between OSes.
- **`# pragma: no cover` on `if IS_WINDOWS:` branches** is the agreed convention; the
  Windows CI job is what exercises them.
- **`OPENBOX_DATA_DIR` still overrides everything**, including on Windows.
- **Don't rename or re-sign the frozen v1 routes.** The only addition is
  `POST /api/v1/import/epic`, already frozen in `v1_contracts.json`.

---

## 11. Suggested order of work

1. Fix the 11 remaining test failures (§7) — start with `test_parity_resume.py`.
2. Add `pkg/platform_compat.py` to `runtime_modules.txt` and write
   `tests/test_platform_compat.py` (§9.1-9.2).
3. Clean up unused imports and the remaining `open()` encoding audit (§9.4-9.5).
4. Build the WebView2 host (§8.1) and skip/replace `test_native_host.py` (§7.7).
5. Add Windows launcher scripts (§8.2) and gate the Linux-only packaging tests (§7.8).
6. Port the gates and add the Windows CI job (§8.5-8.6).
7. Installer, updater, protocol registration (§8.3).
8. Emulator YAML Windows binaries (§8.4).
9. UI + i18n for the Epic importer if it should be user-visible (§9.6).
10. ADR in `docs/adr/` (layout/contracts/gates changed), changelog entry, README Windows
    section, and `docs/SUPPORT.md` update — required before the PR (`AGENTS.md`).

---

## 12. File inventory

### New files (untracked)

- `pkg/platform_compat.py` — the platform shim (§4).
- `scripts/run_windows_tests.py` — dev test runner (§6).

### Scratch files (outside the repo, safe to ignore/delete)

- `C:\Users\theha\AppData\Local\Temp\opencode\scan_encodings.py`
- `C:\Users\theha\AppData\Local\Temp\opencode\scan_writes.py`
- `C:\Users\theha\AppData\Local\Temp\opencode\codemod_encodings.py`
- `C:\Users\theha\AppData\Local\Temp\opencode\codemod_writes.py`
- `C:\Users\theha\AppData\Local\Temp\opencode\windows-test-results.json`

### Modified runtime files (uncommitted)

```
arcade.py                          handlers/moments.py
archives.py                        handlers/sessions.py
cloud_sync.py                      importers.py
crash_report.py                    openbox.py
emulators.py                       pkg/parity/launch_tokens.py
env_config.py                      pkg/parity/parity_deeplinks.py
handlers/extensions.py             pkg/parity/parity_emulator_defs.py
handlers/imports.py                pkg/parity/parity_gamescope.py
handlers/settings.py? (check)      pkg/parity/parity_gameyfin.py
pkg/parity/parity_household.py     pkg/parity/parity_insights.py
pkg/parity/parity_integrations.py  pkg/parity/parity_media.py
pkg/parity/parity_storefront.py    pkg/parity/parity_tracking.py
pkg/state/cache.py                 pkg/state/commands.py
pkg/state/imports.py               pkg/state/launch.py
pkg/state/sse.py                   plugin_catalog.py
plugins.py                         retroachievements.py
routes.py                          saves.py
state_store.py                     static/util.js
v1_contracts.json                  web_app.py
webapp_state.py
scripts/capture_readme_screenshots.py
scripts/check_runtime_modules.py   scripts/check_tests.py
scripts/check_tokens.py            scripts/perf_bench.py
scripts/verify_release.py
```

(`parity_deeplinks.py`, `parity_insights.py`, `parity_integrations.py`, `parity_media.py`,
`parity_storefront.py`, `pkg/state/sse.py`, `plugin_catalog.py`, `plugins.py`,
`retroachievements.py`, `scripts/*` mostly changed only for the `encoding="utf-8"`
codemod — verify each diff is only that.)

Plus ~100 test files changed (encoding codemod + platform guards).

---

## 13. Gotchas discovered

- **`os.kill(pid, 0)` terminates the target on Windows.** This was a live bug: session
  reattach and shutdown would have killed the game. Always use `process_alive`.
- **`selectors.DefaultSelector` cannot poll pipes on Windows** (`WinError 10038`). That is
  why `archives.py` uses a reader thread now.
- **`msvcrt.locking` locks a byte range, not the whole file.** `lock_handle` seeks to 0 and
  locks 1 byte; every writer must do the same (they do, via the helper).
- **Windows `chmod` only toggles the read-only bit.** All `0o600`/`0o700` assertions and
  security assumptions are POSIX-only; that is why `file_is_private` exists.
- **`Path("/tmp")` becomes `\tmp` on Windows.** Test fixtures that pass POSIX paths will
  silently normalize; use real temp dirs in new tests.
- **`shlex.split` treats backslashes as escapes.** `split_command` uses MSVCRT rules on
  Windows; stored commands written on one OS may not round-trip perfectly on the other
  (documented limitation).
- **`tempfile.NamedTemporaryFile` keeps the file open on Windows** (unlike POSIX). Several
  tests needed an explicit `.close()` before `os.unlink`/cleanup.
- **Git reports `LF will be replaced by CRLF`** for every touched file. That is expected
  (`.gitattributes`/`core.autocrlf` behavior); the content is LF in the index.
- **The `[WinError 193]` / `[WinError 2]` errors in test output** are almost always a test
  fixture trying to execute a shell script or `/bin/true`; replace the fixture.
- **`python3` on this machine is a Microsoft Store alias** — use `python` or `py`.

---

## 14. One-paragraph summary for the next agent

The runtime now imports and runs on Windows. A new stdlib-only `pkg/platform_compat.py`
provides file locking, process liveness/termination/suspend/resume, `/proc` equivalents
via ctypes, command quoting, path openers, Steam discovery, and data-dir resolution.
`state_store.py`, `cloud_sync.py`, `parity_household.py`, `parity_media.py`,
`env_config.py`, `pkg/state/launch.py`, `web_app.py`, `parity_tracking.py`,
`parity_gamescope.py`, `importers.py`, `saves.py`, `archives.py`, `crash_report.py`,
`handlers/sessions.py`, `handlers/extensions.py`, `handlers/moments.py`,
`pkg/state/cache.py`, `pkg/state/commands.py`, `pkg/state/imports.py`,
`pkg/parity/launch_tokens.py`, `pkg/parity/parity_emulator_defs.py`, and
`pkg/parity/parity_gameyfin.py` were ported. A new `POST /api/import/epic` route was
added and frozen. 123/134 test files pass on Windows; the remaining 11 failures are
mostly test fixtures still using POSIX paths/scripts plus three real areas
(`parity_resume` adapter resolution, `parity_gamescope` `Popen` stubs, and a SQLite
handle leak in `test_metadata`). Not started: the WebView2 native host, Windows launcher
scripts, installer/updater/`openbox://` registration, Windows emulator YAML binaries,
Windows CI, gate porting, ADR/changelog/README, and tests for the new shim. The two
immediate blockers for the repo gates are adding `pkg/platform_compat.py` to
`runtime_modules.txt` and writing `tests/test_platform_compat.py` (85% floor).
