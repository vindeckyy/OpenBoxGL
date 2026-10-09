# ADR 0071: The native windows paint the saved theme's background first

**Date:** 2026-10-08
**Status:** Accepted (1.16.1); the Linux host is built here, the Windows host is compiled here and linked and run only in CI

## Context

Both native hosts (`native_host.c` on WebKitGTK, `native_host_win.c` on WebView2) set their view's
background to `#11100e` before the page loads. A user on a light theme saw a dark window until the first
paint. ADR 0063 listed this as a known limit.

## Decision

- The app writes the active global theme's page background (`--bg` in the theme CSS, or the built-in
  `#11100e`) to `native-background` in the data folder: once when the theme is selected
  (`handlers/extensions.py` `select_theme`) and once at startup (`web_app.py`). Only a six-digit hex value
  is written.
- Both hosts read the file before the view is created and apply it. A missing or malformed file keeps
  `#11100e`. The hosts never read anything else from it.
- Platform-specific theme mappings do not change the first paint; only the global theme does.

## Evidence

- `tests/test_native_background.py`: the writer (no theme, a theme with a background, a theme without one,
  a path-like name that must not leave the themes folder). It also compiles each host's
  `apply_saved_background` from the real source and runs it against a saved file, a missing file and a
  malformed file. The Linux helper is compiled against GLib. The Windows helper is compiled against
  stand-in WebView2 types, so it checks the parsing and fallbacks, not the Win32 calls.
- **Linux host, built for real.** `native_host.c` compiles with the project's own command
  (`gcc -O2 native_host.c $(pkg-config --cflags --libs webkit2gtk-4.1)`, WebKitGTK 2.52). The one warning
  (an unused parameter) is in the committed version too. `tests/test_native_host.py` runs its 13 tests, and
  the compile test no longer skips.
- **Windows host, compiled but not linked.** `native_host_win.c` compiles to an object file with zig's
  Windows target, using the project's defines (`/DUNICODE /D_UNICODE /D_CRT_SECURE_NO_WARNINGS`), against the
  real WebView2 headers from the Microsoft.Web.WebView2 package. One header, `EventToken.h`, is from the
  Windows SDK, which is not in that package; a stand-in with the SDK's definition was used. The committed
  version compiles the same way. Linking needs MSVC's `uuid.lib`, which is not available here, and nothing has
  been run on Windows. The Windows CI job (MSVC, `scripts/build_native_host_windows.ps1`) is the real check.

## Consequences

- A theme change takes effect at the next launch of the native window, not immediately.
- The file is owner-readable only in the data folder, like the other host files; it holds no secret.
