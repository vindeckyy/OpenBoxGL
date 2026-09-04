# ADR-0001: native host is a WebKitGTK C shim rendering the loopback UI

Status: accepted
Date: 2026-08-13
Deciders: OpenBox maintainers

## Context

OpenBox ships two interfaces over one local core: a full-featured web UI
(`web_app.py` + `index.html` + `static/app.js` + `static/app.css`) and a
lightweight Tk shell (`openbox.py`). The Tk shell is a separate presentation
stack with no cover grid, no Big Box, no themes, and no metadata/media/saves
integrations. The two shells do not look alike and require hand-maintained
feature parity.

The goal for v1.0 is a native app that looks virtually identical to the web
app, so OpenBox can migrate to native-only.

The runtime is stdlib-only by policy (`pyproject.toml`): "The runtime app has
zero third-party dependencies and must stay that way." The AppImage never
installs dev packages.

## Decision

OpenBox has one UI. The native app is a native window that renders the exact
same `index.html`, `app.js`, and `app.css` from the existing loopback server.
The native host is a small C program linking `libwebkit2gtk-4.1` (WebKitGTK
4.1), which loads the token-bearing loopback URL, owns the server lifecycle,
and exposes native dialogs and window chrome through a JS bridge.

The Tk shell is deleted. Native-only for v1.0 is a packaging and default-mode
change, not a port.

## Options considered

| Option | Verdict | Reason |
|---|---|---|
| A. Hardened system-browser app window | Bridge | Already mostly built; ships immediately; depends on a user browser |
| B. C WebKitGTK host | Chosen | Real native window, no user browser, keeps Python stdlib-only |
| C. GJS + WebKitGTK | Fallback | Less universal; only if C host is unbundleable |
| D. Electron / Tauri | Rejected | Breaks zero-dependency spirit, adds toolchain, oversized artifact |
| E. Rewrite UI in Tk/Qt | Rejected | Never pixel-identical, months of work, doubles every future feature |

## Rationale for B over D and E

- B is a C host binary, not a Python import. The stdlib-only rule governs the
  Python runtime; a C shim linking a system library does not violate it.
- B renders the identical HTML/CSS/JS, so the native app is pixel-identical to
  the web app by construction, not by approximation.
- D (Electron/Tauri) breaks the policy for no user-visible gain. E (rewrite in
  a widget toolkit) cannot reach pixel-identical and duplicates every future
  feature's cost.

## Build spike (completed 2026-08-13)

Result: WebKitGTK 4.1 works on the target environment.

- `pkg-config --modversion webkit2gtk-4.1` -> `2.52.3`.
- `libwebkit2gtk-4.1-0` 2.52.3 runtime was already installed; the dev headers
  (`libwebkit2gtk-4.1-dev`) were installed to compile.
- A minimal C program linked with `pkg-config --cflags --libs webkit2gtk-4.1`
  compiled clean.
- Run headless on the live display, it rendered a local HTML page and printed
  `BRIDGE GOT: bridge-ready` from the WebKit `script-message-received` handler,
  proving both page load and the JS-to-C bridge.

## Bundling decision

The AppImage links the system `libwebkit2gtk-4.1`. It is present on the target
system and in the Flatpak `org.freedesktop.Platform` runtime. The host prints a
one-line install hint and falls back to the hardened system-browser window when
the library is absent.

## Consequences

- Positive: one renderer, two hosts; feature parity is free; the Python runtime
  stays stdlib-only.
- Negative: a small amount of C to maintain (now ~1000 lines in `native_host.c`, grown past the original ~600-line budget as dialogs, gamepad stub, and single-instance handling landed); a system
  dependency on WebKitGTK.
- Neutral: the loopback server remains the application core; only the product
  surface changes.
