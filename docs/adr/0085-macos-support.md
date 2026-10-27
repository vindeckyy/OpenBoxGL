# ADR 0085: macOS support

**Date:** 2026-10-27
**Status:** Accepted

## Context

Every channel was Linux-first and then Windows: the native window was a
WebKitGTK program, then a WebView2 one; the data lived under
`~/.local/share`, then `%LOCALAPPDATA%`; process identity came from
`/proc`, then from the Windows process table. macOS was declared out of
scope in `AGENTS.md` and `CONTRIBUTING.md`, and `pkg/platform_compat.py`
carried a few macOS branches that no test ever reached.

macOS is not Linux with a different package manager. It has no `/proc`
(the process table is libproc), no `/usr/lib` layout, case-insensitive
volumes, decomposed file names, app bundles instead of bare executables,
a different desktop integration, and a different trust model for
distributed binaries. Porting it therefore means adding a third
platform seam, not porting the Linux one.

## Decision

1. **`pkg/platform_compat.py` stays the only platform seam.** macOS
   process identity, the data directory, the Steam layout, the desktop
   openers, the battery reader, the screenshot tool, the app-bundle
   resolver, the path-identity folding, and the symlink rules all live
   there. No runtime module gained a `sys.platform` check.

2. **Process identity comes from libproc.** `proc_pidinfo` with
   `PROC_PIDTBSDINFO` gives the parent pid and the start time,
   `proc_pidpath` gives the executable, and `PROC_PIDVNODEPATHINFO`
   gives the working directory. The start token is
   `"<seconds>.<microseconds>"`, the command fingerprint is the
   executable path (as on Windows), and a struct-size assertion makes
   the layer fail closed rather than mis-parse on a future macOS.

3. **The data directory is `~/Library/Application Support/openbox-game-launcher`**,
   with `OPENBOX_DATA_DIR` still overriding everything. Stored
   references keep POSIX separators, so a library moves between
   platforms unchanged.

4. **The native window is `native_host_mac.m`** (Objective-C, AppKit and
   WebKit), mirroring `native_host.c` and `native_host_win.c`: it spawns
   the Python child with `posix_spawn` in its own process group, waits
   for `server.port` and `server.token`, injects the same
   `window.openboxNative` bridge, serves the tray from an `NSStatusItem`,
   forwards `openbox://` deeplinks, and owns the teardown. The
   `AF_UNIX` single-instance socket checks the 104-byte `sun_path` limit
   before it binds, so a long data-directory path degrades to "no single
   instance" instead of connecting to the wrong socket.

5. **The app bundle ships its own interpreter.** `OpenBox.app` carries
   a universal CPython 3.12 (python-build-standalone, pinned by
   SHA-256) and the runtime payload, so a user needs nothing installed.
   The bundle is ad-hoc signed, not notarized: the first launch asks
   for one user action in Privacy & Security. The release signature
   (Ed25519 plus a SHA-256 sidecar) is unchanged and is what the
   installer and the updater verify.

6. **One universal artifact.** `OpenBox-macos.zip` carries an arm64 and
   an x86_64 host and one merged interpreter. The updater replaces the
   bundle in place through a detached applier once the server has
   exited, and keeps the previous bundle as `OpenBox.app.previous`.

7. **Emulator definitions carry `native_exe_macos`.** The key is
   required by the definition gate and by the update channel, exactly
   as `native_exe_windows` is (ADR 0048). A value is an app bundle name
   (`RetroArch.app`), a bare program (`mame`), or empty when no macOS
   build is verified. The bundle is resolved through
   `Contents/Info.plist`, and the executable inside it is launched
   directly so the emulator's own pid is tracked.

8. **What macOS does not have is hidden, not broken.** Heroic, Lutris,
   Epic, the Flatpak emulator channel, Faugus, Proton scanning,
   gamescope and Game Mode have no macOS implementation, so their
   controls are hidden there. Game Mode is refused with a clear
   message. The plugin sandbox stays unavailable (macOS has no
   bubblewrap), which is the documented no-bwrap rule.

9. **macOS is a first-class CI target.** The `macos` CI job runs the
   suite and the portable gates on `macos-15` and `macos-15-intel`, and
   builds and smoke-tests the bundle. The release job signs the bundle
   with the same key as every other artifact.

## Consequences

- A macOS user installs OpenBox by downloading `OpenBox-macos.zip` (or
  running `scripts/install-macos.sh`), and gets a real window, the
  tray, native dialogs, `openbox://` deeplinks, and the in-app updater.
- The first launch needs one user action, because the bundle is not
  notarized. Every build also resets the ad-hoc signature, so macOS may
  ask again for the folders, the screen recording, and the network
  volumes the user granted before.
- The bundled interpreter is a build input with a pinned hash; the
  runtime stays stdlib-only, and the CA bundle it trusts is a pinned
  certifi wheel copied in at build time.
- The macOS emulator set is the one the upstream projects actually
  ship. Definitions with no verified macOS build are reported as not
  installed instead of failing at launch.
- The Linux and Windows behaviour is unchanged. Every macOS branch is
  marked `# pragma: no cover` and exercised by the macOS CI job, and
  the shared helpers are covered by the Linux gate.
