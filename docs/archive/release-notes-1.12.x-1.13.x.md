> Archived release notes for OpenBox 1.12.x–1.13.x, recovered from the v1.13.1 tag after the cumulative RELEASE_NOTES.md was replaced by the 1.14.0 notes.

# OpenBox 1.13.1 — The Windows debut, made solid

A strict patch release: no new features, just regression fixes, hardening,
and audits on top of 1.13.0's Windows debut. Every fix below is pinned by a
regression test.

---

## Fixed

- **Late-night moments filed under the wrong day.** Timezone-aware timestamps
  (stored in UTC, e.g. moments) were grouped by their UTC calendar date in the
  story view and the history timeline. Both now convert to the viewer's local
  day first, so a moment captured at 23:30 local sits with its session instead
  of the next day.
- **Hidden tabs overwrote newer state.** A background tab holding stale
  `AppState` could send state-changing requests that clobbered what the
  visible tab had written. Mutations from hidden tabs are now blocked (shutdown
  excepted), and SSE streams close when the tab hides.
- **SSE subscriber cap opened dead streams.** When every event slot was taken,
  the server opened a stream that only ever sent heartbeats. It now answers
  `503 SSE_BUSY` with `Retry-After`, and session reconnects close the previous
  `EventSource` instead of leaking it.
- **Windows installer left half-registered installs.** If Start Menu or
  `openbox://` protocol registration failed, the installer now restores the
  previous installation tree. The WebView2 SDK hash is pinned and verified at
  build time.
- **Native host repairs.** "Reveal in Explorer" passed the folder in the wrong
  argument position so it always failed; rejected or malformed WebView2 bridge
  messages left the pending frontend promise hanging instead of rejecting it.
- **Dialog and wizard consistency.** Lazily created dialogs (prompt, choice,
  confirm, Trophy Case) now share the focus-restoration wiring of static
  dialogs; click-outside dismissal routes through the shared close path; every
  Setup Center open path goes through the single refresh-and-render entry
  point; three untranslated strings now go through i18n in all five locales.
- **Trust chain and update verification.** Valid signatures over wrong bytes,
  tampered artifacts, and missing signatures are all rejected and covered by
  tests; the CLI fails loudly with a machine-readable marker.
- **Twenty previously shipped fixes pinned.** The full Unreleased fix list from
  1.13.0 is now covered by regression tests: session token survival, import
  batch projection, job titles, static cache headers, settings schema, and
  more.

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |
| `OpenBox-x86_64-windows.zip` | x86_64 | Windows portable (signed) |
| `OpenBox-x86_64-windows-native-host.exe` | x86_64 | WebView2 window host (signed) |

**Windows:** download `install.ps1` and `OpenBox-x86_64-windows.zip` from this
release and run the installer in Windows PowerShell 5.1. **Linux:** pick the
AppImage that matches your CPU, or install the Flatpak. Already running
OpenBox? Use the built-in updater or download the matching artifact from the
release page.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.13.0...v1.13.1

---

# OpenBox 1.13.0 — Windows

OpenBox now runs natively on Windows alongside Linux: the same library and the
same UI in a real WebView2 window, installed from a signed portable release with
verified updates. The Linux channels are unchanged.

---

## What's New

### A native Windows window

`native_host_win.c` is a full WebView2 host: it spawns the same `web_app.py`
loopback server, waits for the token and port, and renders the one UI in a
WebView2 window with the same `window.openboxNative` bridge the page already
speaks — remembered window geometry, tray icon and minimize-to-tray,
`openbox://` deeplinks, and one instance per data directory that focuses the
window already open. On close it stops the server gracefully (`CTRL_BREAK`) and
force-kills the tree through a job object if the server does not exit.

The release workflow builds `native_host.exe` on `windows-latest` from
`native_host_win.c` (Visual Studio Build Tools with the C++ workload, plus the
WebView2 SDK from the NuGet cache or nuget.org), refuses to publish a zip that
does not contain it, and the `windows-latest` CI job compiles the same host on
every push — so the released zip runs the native window out of the box. Source
checkouts build it with `scripts/build_native_host_windows.ps1`; when the binary
is absent the launchers open the same UI in a browser app window.

The same binary is attached to this release as
`OpenBox-x86_64-windows-native-host.exe` — attested and signed with the release
key, exactly like the zip it also ships inside — so a source checkout that does
not want to install MSVC and the WebView2 SDK can save it beside `web_app.py` as
`native_host.exe` (or point `OPENBOX_NATIVE_HOST` at it) and get the native
window. It is the window host only: the rest of the tree stays the source
checkout's, and without the WebView2 runtime `--web` runs the browser UI.

### Launchers and a verified install

`openbox.cmd`, `openbox.ps1`, and `openbox-native.ps1` follow the same ladder as
the shell scripts: native host first, then the browser app window, then a plain
tab; `openbox --web` forces the browser. `scripts/install.ps1` verifies the
release key anchor, the SHA-256 sidecar, and the Ed25519 signature *before*
extracting anything, installs to `%LOCALAPPDATA%\OpenBox\share\openbox`, keeps
the previous tree at `share\openbox.previous`, adds the bin root to your user
`PATH`, and registers both a Start Menu shortcut and the `openbox://` protocol
handler. The in-app updater verifies and swaps the installed tree the same way,
and refuses to overwrite anything that is not an installed copy.

### Windows-aware library behavior

Every bundled emulator definition carries its Windows executable name, so
adapter detection, resume state, and Launch Doctor work with Windows builds of
Dolphin, RetroArch, PCSX2, RPCS3, Cemu, melonDS, PPSSPP, Vita3K, xemu, Xenia,
and the rest. Library state lives in `%LOCALAPPDATA%\openbox-game-launcher\`,
and stored references keep POSIX separators so a library stays portable between
platforms. Windows builds of Steam libraries are discovered through the same
import path as Linux.

### Still no dependencies

The runtime stays stdlib-only, `ctypes` included: file locking, process
liveness/suspend/terminate, `/proc` equivalents, command quoting, path openers,
browser launching, and data-directory resolution all live behind
`pkg/platform_compat.py`, the single platform seam (ADR 0048). The AppImage,
Flatpak, and system-install paths are untouched.

---

## Fixed

- **A liveness check that killed games.** `os.kill(pid, 0)` is not a probe on
  Windows — CPython maps it to `TerminateProcess`, so reattaching to a running
  game or requesting shutdown could kill it. Liveness now goes through
  `platform_compat.process_alive`.
- **Stored references use POSIX separators everywhere.** SBOM symlink targets,
  `{EmulatorDir}` token parents, resume-state metadata, and the runtime-module
  manifest are written with `/` on Windows instead of backslashes, so artifacts
  and state compare correctly across platforms.
- **SQLite handles before an atomic swap.** The metadata database closes its
  cached thread-local connections before replacing the file, so a live resync
  cannot fail with a locked `metadata.db` on Windows.
- The standalone changed-line/touched-module checker now honors ADR 0025:
  test and script edits no longer count as coverage misses, and a touched
  module fails only at 0% instead of an unenforceable whole-file 95%.

## Windows boundaries

Linux remains the primary target for distro integration: AppImage and Flatpak
packaging, gamescope/Game Mode, XDG desktop entries, and Flathub-aware emulator
management are Linux-only. Windows ships x86_64 and the released zip includes the
compiled WebView2 host, so the native window works as installed; the WebView2
runtime is required (present on Windows 11 and most Windows 10 systems), and
`--web` runs the browser UI if it is missing.

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |
| `OpenBox-x86_64-windows.zip` | x86_64 | Windows portable (signed) |
| `OpenBox-x86_64-windows-native-host.exe` | x86_64 | WebView2 window host (signed) |

**Windows:** download `install.ps1` and `OpenBox-x86_64-windows.zip` from this
release and run the installer in Windows PowerShell 5.1; it needs no `curl` and
no OpenSSL. Running from a checkout instead? Take
`OpenBox-x86_64-windows-native-host.exe` and save it beside `web_app.py` as
`native_host.exe`. **Linux:** pick the AppImage that matches your CPU, or
install the Flatpak for a sandboxed desktop setup. AppImages are signed and
include SHA-256 checksums, zsync metadata for delta updates, and SBOMs. Verify
with `openbox-release.pub` and `install.sh`.

Already running OpenBox? Use the built-in updater or download the matching artifact from the release page.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.12.1...v1.13.0

---

# OpenBox 1.12.1 — Hardening

A reliability pass: no new features, just a sturdier launcher.

---

## Fixed

- **Update verification:** the Ed25519 check now rejects the full
  small-order point blacklist (orders 1, 2, 4 and 8, matching the
  libsodium/ZIP-215 set), a malformed GitHub releases payload fails closed
  instead of crashing, and a symlinked AppImage path updates the real file.
- **Import honesty:** a Steam library on a read-only mount is reported in
  the import result's `errors` list with the actual path instead of being
  skipped silently.
- **Clearer failure surfaces:** RetroAchievements 401/403 responses read
  "RetroAchievements rejected those credentials", and an unreachable
  metadata database reports a connection error in the job panel instead of
  a raw socket message.
- **Every reliability scenario is now gated:** the last manual rows in
  `docs/reliability.md` (non-UTF8 paths, offline sync, read-only mounts,
  bad credentials, long names, delete-while-open, rapid filtering) are
  covered by automated tests, and coverage floors were ratcheted to
  83% / 58% (web_app).
- **Honest empty states and errors:** deleting a playlist that doesn't exist
  returns 404 instead of a false success; scoped library exports without a
  name are rejected synchronously with 400; and an empty Game Night queue
  explains itself with an `empty_reason` plus an exclusion breakdown
  (e.g. no games support N players) instead of a bare empty state.
- **Typed background jobs:** export, SteamGridDB, ScreenScraper,
  auto-import, and reel jobs now run as `library.export`,
  `screenscraper.*`, `steamgrid.*`, `storefront.auto_import`, and
  `clips.reel` with the correct retry policy instead of masquerading as
  `setup.scan`.
- **Backlog Radio estimates that survive reloads:** picks hydrated from the
  stored playlist now carry `estimated_minutes` (with a frontend fallback
  for older entries), fixing "undefinedm" rows.
- **Test isolation:** the suite no longer risks the real library — test runs
  export an isolated `OPENBOX_DATA_DIR` (and `test_sse.py` guards itself at
  import time), so synthetic games can't land in
  `~/.local/share/openbox-game-launcher/library.json`.

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |

Already running OpenBox? The built-in updater handles the delta.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.12.0...v1.12.1

---

# OpenBox 1.12 — Living Library

OpenBox 1.12 makes the 1.11 feature wave feel finished: searches you pinned
become shelves that stay correct on their own, every game gets a story worth
scrolling, and the launch-options sheet is complete down to environment
variables.

---

## What's New

### Smart Collections

The search bar's deterministic grammar (`short unplayed rpg`, `beaten co-op
before 2000`) can now be pinned. "Save as collection" turns the active query
into a named sidebar shelf that re-evaluates live — a collection stores the
question, not the answer, so "under 5 hours, never played" stays honest as
your library and habits change.

### Game Story

A new Story tab in the detail pane narrates each game's journey: added to the
library, first played, longest session, playtime milestones, progress states,
and the Moments you captured — one deterministic timeline built from the
session journal, no separate tracking required.

### Per-game Environment Overrides

Edit game → Launch gains `KEY=value` environment overrides per title, merged
over the launch environment at spawn. Combined with the existing per-game
launch command, profile, and Gamescope preset, every launch knob is now
per-game.

### Fixed

- The per-game **Gamescope preset override** saved correctly but was never
  projected to the client, so the Edit game select always rendered blank.
  It now round-trips.

Plus the full 1.11.1 hardening sweep already on master: complete large-media
responses, document-reader framing, scroll-stable grid virtualization, and
focus restoration fixes.

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |

Choose the AppImage that matches your CPU, or install the Flatpak for a sandboxed desktop setup. AppImages are signed and include SHA-256 checksums, zsync metadata for delta updates, and SBOMs. Verify with `openbox-release.pub` and `install.sh`.

Already running OpenBox? Use the built-in updater or download the matching artifact from the release page.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.11.0...v1.12.0
