# Native host contract

The native host renders the same `index.html`, `app.js`, and `app.css` as the
web UI, served by `web_app.py` over loopback. This document is the contract
between the page and the host. Both directions must keep working when the host
is absent, so the web UI remains usable in a plain browser for development.

## Platform implementations

Two hosts implement this contract: `native_host.c` (WebKitGTK, Linux, built by
`make native-host`) and `native_host_win.c` (WebView2, Windows, built by
`scripts\build_native_host_windows.ps1`). A third one, `native_host_mac.m`
(WKWebView, macOS, built by `scripts/build_native_host_macos.sh`), implements
it the same way. Where a clause below names a POSIX mechanism, the WebView2
host uses the equivalent Windows mechanism:

| Clause | Linux (WebKitGTK) | Windows (WebView2) | macOS (WKWebView) |
|---|---|---|---|
| Single instance | `AF_UNIX` socket in the data directory | named pipe opened with `FILE_FLAG_FIRST_PIPE_INSTANCE` (the atomic guard), same `focus`/`deeplink` forwarding | `AF_UNIX` socket in the data directory, with the 104-byte `sun_path` limit checked before it binds |
| Graceful stop of the Python child | `SIGTERM` to the child process group | `CTRL_BREAK` to the `CREATE_NEW_PROCESS_GROUP` child | `SIGTERM` to the child process group (the child is started with `posix_spawn` and `POSIX_SPAWN_SETPGROUP`) |
| Force-kill backstop | `SIGKILL` to the process group | job object (`TerminateJobObject`), which also kills children | `SIGKILL` to the process group |
| Geometry persistence | `window-geometry`, `"<w> <h> <maximized>"` | same file, same format | same file, same format |
| Tray flags | `native-host-flags` | same file | same file |
| Tray recent games | `native-host-recent`, eight most recent | same file, same limit | same file, same limit |
| Startup background | `native-background` (`#rrggbb`) painted before page load | same file, same format | same file, same format |
| `gamepad` capability | `"webkit"` (fixed; the page always uses the Web Gamepad API) | same | same |

The bridge object, the HTTP native surface, the capability shapes, and the
browser fallbacks are identical on both hosts; `handlers/native.py` produces the
capabilities either way.

## Host entry

The host spawns `web_app.py --no-browser` as a child, reads `server.port` and
`server.token` from the data directory, then loads:

```
http://127.0.0.1:<port>/?token=<token>
```

The page reads the token from `location.search` (the same mechanism the web UI
already uses) and sends it as `X-OpenBox-Token` on API calls. The host injects
no token into the DOM; it only supplies the URL.

## The bridge object

When a native host is connected, it injects a `window.openboxNative` object
before page load. The page must treat it as optional and never throw when it is
absent. Every capability has a browser fallback.

```js
window.openboxNative = {
  // Show a native file/folder/save dialog. Resolves with the chosen path or
  // { cancelled: true }.
  dialog(kind, opts) -> Promise<{ path: string|null, cancelled: boolean }>,

  // Open a path or URL with the default handler (xdg-open / GIO).
  openExternal(pathOrUrl) -> Promise<{ ok: boolean }>,

  // Reveal a path in the file manager.
  reveal(path) -> Promise<{ ok: boolean }>,

  // Apply window chrome: "minimize" | "toggle-maximize" | "close" |
  // "set-fullscreen" | "unset-fullscreen".
  windowAction(action) -> Promise<{ ok: boolean }>,

  // No-op stub: the host never feeds gamepad state, so the page must keep
  // using the Web Gamepad API (`pollGamepads` edge detection). `gamepad`
  // in capabilities is always "webkit".
  onGamepad(callback) -> void,
};
```

The bridge is a direct path; the HTTP surface below is the fallback that works
even when bridge injection is unavailable, and it is what the tests exercise.

## HTTP native surface

All native routes are authenticated like every other route: unauthenticated calls get `403` with the shared `{"error":"Unauthorized"}` response. Authorized calls when no native host is attached return capability-absent fallbacks (never an error that blocks the browser fallback).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/native/dialog` | folder/file/save picker |
| POST | `/api/native/open-external` | open a path or URL |
| POST | `/api/native/reveal` | reveal a file in the file manager |
| POST | `/api/native/window` | window chrome actions |
| GET | `/api/native/capabilities` | report host capabilities |
| GET | `/api/native/battery` | battery presence, percentage, and charging state |

`GET /api/native/capabilities` returns:

```json
{
  "webview": true,
  "dialogs": true,
  "tray": true,
  "single_instance": true,
  "gamepad": "webkit",
  "fullscreen": true,
  "clipboard": true,
  "version": "1.19.1",
  "platform": "linux",
  "data_dir": "/home/user/.local/share/openbox-game-launcher",
  "features": {
    "gamescope": true,
    "mangohud": true,
    "handheld_perf": true,
    "flatpak": true,
    "wine": true,
    "faugus": true,
    "lutris": true,
    "heroic": true,
    "epic": true,
    "native_storefronts": false,
    "desktop_entry": true
  }
}
```

`version` is the running OpenBox version, `platform` is `sys.platform`, and
`data_dir` is the application directory; Settings > About reads all three.
`webview`, `dialogs`, `tray`, and `single_instance` are true only when the
Python child was started by a native host (`OPENBOX_NATIVE_HOST`). `fullscreen`
and `clipboard` are always true.

`features` is the platform capability contract (ADR 0086). The server computes
it from the platform flags in `pkg/platform_compat.py`; the frontend shows each
platform-specific control only when its key is `true`. A missing `features`
object (an older server, or a failed fetch) means every key is false and every
gated control stays hidden. Feature keys and the controls they gate are listed
in [ADR 0086](adr/0086-platform-capability-contract.md).

`GET /api/native/battery` reads the first `BAT*` entry under
`/sys/class/power_supply` on Linux, reads `pmset -g batt` on macOS, and
reads `GetSystemPowerStatus` on Windows. It returns
`{"present": bool, "percent": 0-100, "charging": bool}`, where `charging`
reports the charging state on each platform. A host with no battery, such as a
desktop, returns `{"present": false, "percent": 0, "charging": false}`. The
Windows and macOS readings are cached for 30 seconds. The response never
includes the sysfs path. Big Box reads it for its battery indicator.

When no host is connected, the native endpoints return a capability-absent
result (never an error that blocks the browser fallback): `dialog` returns
`{ "cancelled": true }`, `open-external` and `reveal` return `{ "ok": false }`,
and `window` returns `{ "ok": false }`.

## Deeplinks

The host loads the page with `?token=<token>` plus an optional `deeplink`
action. The page reads these from `location.search` and dispatches them on the
first render:

| Query | Behavior |
|---|---|
| `?deeplink=showgame&id=<game id>` | Selects the game and scrolls its card into view |
| `?deeplink=resume&id=<game id>` | Selects the game and scrolls its card into view, after the host has resumed the session server-side |
| `?deeplink=launch&id=<game id>` | Starts the game through the normal launch path (the tray's recent-games items use this) |
| `?deeplink=search&q=<text>` | Seeds the library search box |
| `?deeplink=bigbox` | Opens Big Box |
| `?deeplink=settings` | Opens Settings |
| `?deeplink=moment&id=<game id>` | Opens that game's moment |
| `?deeplink=clip&id=<game id>` | Opens the clip for that game |
| `?deeplink=recap` | Opens the session recap |

The host builds these URLs itself and accepts the actions below from its
command line and from the `openbox://` URI scheme (`recap` is page-only; the
host does not emit it):

- `--play <game id>` starts a game directly.
- `--uri openbox://<action>[/<value>]` maps to the table above, e.g.
  `openbox://showgame/42`, `openbox://search/half%20life`,
  `openbox://settings/audio`, `openbox://fullscreen` (Big Box).
- `openbox://launch/<game id>` and `openbox://resume/<game id>` call
  `POST /api/launch` and `POST /api/v2/resume` on the authenticated server,
  then load the window at `?deeplink=showgame&id=` or `?deeplink=resume&id=`.
- A second instance forwards its request to the running instance through the
  single-instance channel and exits; the window is raised and the deeplink
  dispatched there. Invalid or hostile URIs (another scheme or host) are
  rejected and never forwarded.

Game ids are escaped as one URI component and again when the page parses
them, so an id with spaces, `&`, apostrophes, or accents survives the round
trip. The host never interpolates a raw game id into JavaScript.

## Browser-tab assumptions and their native mapping

The page currently assumes a full browser tab in these places. Each must route
through the capability resolver with a browser fallback.

| Browser API | Native mapping |
|---|---|
| `window.open(url)` (manuals, Wikipedia) | `openExternal(url)` |
| `prompt(...)` (removed across the frontend; now styled in-page dialogs) | `dialog("file"/"folder")` or styled in-page prompt |
| `confirm(...)` (removed; was 13 sites, now styled in-page confirms) | styled in-page confirm or native dialog |
| `localStorage` (UI prefs) | server-persisted `ui_state` when native; `localStorage` in browser |
| `navigator.clipboard` | host clipboard; browser fallback |
| `document.fullscreenElement` / request/exit | `windowAction("set-fullscreen"/"unset-fullscreen")` |
| `navigator.getBattery` | `GET /api/native/battery` (Linux sysfs, macOS `pmset`, Windows `GetSystemPowerStatus`); hide status when `present` is false |
| `navigator.getGamepads` | Web Gamepad API always (host `onGamepad` is a no-op stub; capabilities report `"webkit"`) |
| `location.search` (token, deeplink) | unchanged; the host loads the same URL |
| `beforeunload` (shutdown) | page requests `/api/shutdown` when tracked games remain; the native host signals the Python child on window close — `SIGTERM` on Linux, `CTRL_BREAK` on Windows (see Server lifecycle) |

## Server lifecycle

- The host is the parent; it owns the Python child.
- On window close, the page requests `POST /api/shutdown` when tracked games
  remain, while the native host signals the Python child as the lifecycle
  owner: on Linux and macOS it sends `SIGTERM` to the child process group; on
  Windows (`native_host_win.c`) it sends `CTRL_BREAK` to the
  `CREATE_NEW_PROCESS_GROUP` child.
- `web_app.py` performs graceful teardown by stopping tracked sessions, waiting
  up to two seconds, force-killing known game process groups (or their PIDs),
  persisting final cleanup, and draining webhooks. The native host also waits
  for the child and, on Linux and macOS, sends `SIGKILL` to its process group
  if it remains alive; on Windows (`native_host_win.c`) the backstop is
  `TerminateJobObject` on the child's job object, which also kills children.
- The host holds no credentials. The token stays in the URL and the
  owner-readable `server.token` file, unchanged from the current threat model.

## Persistence

Window geometry lives in a flat `window-geometry` file in the data directory
(`native_host.c` `load_geometry`/`save_geometry`), not in `ui_state` and not in
`settings`. The file holds size plus maximized state only (`"<w> <h> <maximized>"`);
there is no x/y — on Wayland absolute window position is WM-controlled and never
persisted. `state_store.py` owns the `ui_state` schema default (`{}`), which carries
UI prefs such as the last session (`view`, `selected_id`, `platform`, `playlist`),
never the native window frame. `single_instance` is a capability reported by
`GET /api/native/capabilities` (`handlers/native.py`), not stored state.

Two more host-owned files sit beside the geometry file:

- `native-background` holds the active theme's page background as `#rrggbb`
  (default `#0F0E0C`). The app rewrites it when the theme changes; both hosts
  paint it as the window background before the page loads so a light theme
  does not flash dark.
- `native-host-recent` holds at most eight lines of `display name<TAB>game id`,
  most recent first. `web_app.py` writes it at boot from play history, with the
  most recently played games first and `last_played` filling any remaining
  slots. The Linux tray and the Windows tray menu both read it and launch a
  listed game through `?deeplink=launch&id=<id>`. The file is owner-only; names
  have tabs and line breaks removed because the host splits on one tab.

`native-host-flags` carries the tray settings written at boot:
`"<tray_enabled> <minimize_to_tray>"`. When `minimize_to_tray` is on, the
window's close button hides the window instead of exiting.
