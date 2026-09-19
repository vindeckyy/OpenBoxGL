# ADR 0048: Windows support

**Date:** 2026-09-19
**Status:** Accepted

## Context

Every channel was Linux-only: the native window was a WebKitGTK/GTK3 C
program, packaging was AppImage plus a Flatpak bundle, desktop integration
wrote XDG `.desktop` files, data lived under `~/.local/share`, and process
liveness, file locking, `/proc` reads, command quoting, and Steam discovery
all assumed POSIX. Porting to Windows had to keep the Linux channels
byte-identical, keep the runtime dependency-free (stdlib plus `ctypes`, so no
`psutil`, `portalocker`, or `pywin32`), keep the v1 route surface frozen, and
keep every existing gate meaningful.

## Decision

1. **`pkg/platform_compat.py` is the only platform seam.** File locking
   (`msvcrt.locking` byte-range locks on Windows), process liveness,
   termination, suspend/resume, `/proc` equivalents via `ctypes`, command
   quoting (MSVCRT rules on Windows), path openers, Steam library discovery,
   browser launching, and data-dir resolution live there and nowhere else.
   `os.kill(pid, 0)` is banned in the runtime: on Windows it *terminates* the
   target, so liveness always goes through `process_alive`.
2. **Windows library data is `%LOCALAPPDATA%\openbox-game-launcher`**, the
   sibling of `~/.local/share/openbox-game-launcher`. `OPENBOX_DATA_DIR` still
   overrides everything, and stored relative references keep POSIX separators so
   a library moves between platforms unchanged.
3. **The native window is `native_host_win.c`** (WebView2, built with MSVC),
   mirroring `native_host.c`: it spawns `python.exe -B web_app.py
   --no-browser`, waits for `server.port`/`server.token`, injects the same
   `window.openboxNative` bridge, serves the same `native-host-flags` and
   `window-geometry` files (both in the data dir), keeps one instance per data
   dir through a named pipe whose first-instance-only creation is the atomic
   guard (forwarding `focus` and `deeplink`), and owns teardown: `POST
   /api/shutdown`, then `CTRL_BREAK`, with the job object as the backstop.
   `scripts/build_native_host_windows.ps1` builds it, linking
   `WebView2LoaderStatic.lib` so no DLL ships beside it.
4. **Windows launchers mirror the bash ladder**: `openbox.ps1` (native host,
   else web app), `openbox.cmd` (double-clickable wrapper), and
   `openbox-native.ps1` (native host, else web app). Share-dir resolution is
   `$OPENBOX_SHARE`, the script's own directory, `..\share\openbox`, then
   `%LOCALAPPDATA%\OpenBox\share\openbox`. Because the host is a GUI-subsystem
   binary, it is started with `Start-Process -Wait -PassThru` — `&` neither
   waits nor sets `$LASTEXITCODE`, which would make the fallback decision
   meaningless.
5. **The update channel is a signed portable zip.** Release assets
   `OpenBox-<arch>-windows.zip`, `.sha256`, and `.sig` are produced by the same
   release job and signed with the same Ed25519 key as the AppImages.
   `scripts/install.ps1` mirrors `scripts/install.sh`'s ladder (key anchor
   pinned to the committed key's SHA-256, sidecar digest, then signature) and
   installs the whole tree into `%LOCALAPPDATA%\OpenBox\share\openbox`, keeping
   the previous tree as `openbox.previous`. The in-app updater refuses to
   replace anything but an installed tree, and swaps it with a detached
   PowerShell applier once the server has stopped.
6. **Desktop integration is `updates.py install-desktop-entry`**: a Start Menu
   `.lnk` created through `WScript.Shell`, plus `openbox://` registered under
   `HKCU\Software\Classes\openbox` with `URL Protocol`. `openbox.desktop`
   remains the Linux contract.
7. **Emulator definitions carry `native_exe_windows`** for every emulator, so
   adapter prefix detection finds Dolphin, RetroArch, PCSX2, RPCS3, and the
   rest under their Windows binary names.
8. **CI runs both platforms.** `.github/workflows/ci.yml` gained a
   `windows-latest` job that runs `scripts/run_windows_tests.py` (per-file
   pass/fail/timeout, non-zero on any failure), builds `native_host_win.c`
   against the WebView2 SDK (`scripts/build_native_host_windows.ps1`, the only
   automated proof that the host still compiles), and runs the portable gates:
   runtime modules, v1 contract, version sync, i18n, CSP, and design tokens.
   `.github/workflows/release.yml` (renamed from `release-appimage.yml`, which
   no longer described its contents) builds, attests, and signs the Windows zip
   beside the AppImages. Gates that need bash or Node (`check_tests.py`'s
   coverage stage, `check_frontend.py`, shellcheck, Flatpak validation) stay
   Linux-only.
9. **Windows-only branches are `# pragma: no cover`**, and the `windows-latest`
   CI job is what exercises them. Coverage floors ratchet up only, so the port
   does not buy its platform with a lower bar.

## Consequences

- The same source tree, tests, and gates now serve Windows; `tests/` is 135
  files and passes in full on Windows.
- Windows users install from a signed zip rather than an AppImage, and get a
  real window through WebView2 instead of a browser tab; `--web` still forces
  the browser.
- The released zip carries a compiled `native_host.exe` built by the release job
  (MSVC plus the WebView2 SDK on `windows-latest`), so a Windows install opens
  the native window with no toolchain. The same build runs in the
  `windows-latest` CI job on every push, and the release asserts the binary is
  inside the archive before signing, so a broken `native_host_win.c` fails the
  build instead of shipping. Source checkouts (no compiled host beside
  `web_app.py`) fall back to the same UI in a browser app window.
- `scripts/run_windows_tests.py` is a dev tool, deliberately absent from
  `runtime_modules.txt`.
- Stored launch commands use platform quoting, so a command written on one
  platform may not round-trip byte-for-byte to the other. The POSIX quoting
  rules are `shlex`'s; Windows follows MSVCRT. This is a documented limitation,
  not a bug to fix by guessing.
- `python3` on Windows is a Microsoft Store alias that does not run scripts;
  tooling invokes `python` (dev venv) so it works on both platforms.
- The Flatpak bundle, the AppImage library-scope test, and the staged-install
  locale test remain POSIX-only and skip on Windows; they are unchanged on
  Linux.
