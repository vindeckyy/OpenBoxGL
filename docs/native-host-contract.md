# Native host contract

The native host renders the same `index.html`, `app.js`, and `app.css` as the
web UI, served by `web_app.py` over loopback. This document is the contract
between the page and the host. Both directions must keep working when the host
is absent, so the web UI remains usable in a plain browser for development.

## Platform implementations

Two hosts implement this contract: `native_host.c` (WebKitGTK, Linux, built by
`make native-host`) and `native_host_win.c` (WebView2, Windows, built by
`scripts\build_native_host_windows.ps1`). Where a clause below names a POSIX
mechanism, the WebView2 host uses the equivalent Windows mechanism:

| Clause | Linux (WebKitGTK) | Windows (WebView2) |
|---|---|---|
| Single instance | `AF_UNIX` socket in the data directory | named pipe opened with `FILE_FLAG_FIRST_PIPE_INSTANCE` (the atomic guard), same `focus`/`deeplink` forwarding |
| Graceful stop of the Python child | `SIGTERM` to the child process group | `CTRL_BREAK` to the `CREATE_NEW_PROCESS_GROUP` child |
| Force-kill backstop | `SIGKILL` to the process group | job object (`TerminateJobObject`), which also kills children |
| Geometry persistence | `window-geometry`, `"<w> <h> <maximized>"` | same file, same format |
| Tray flags | `native-host-flags` | same file |
| `gamepad` capability | `"webkit"` (fixed; the page always uses the Web Gamepad API) | same |

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

`GET /api/native/capabilities` returns:

```json
{
  "webview": true,
  "dialogs": true,
  "tray": true,
  "single_instance": true,
  "gamepad": "webkit",
  "fullscreen": true,
  "clipboard": true
}
```

When no host is connected, the native endpoints return a capability-absent
result (never an error that blocks the browser fallback): `dialog` returns
`{ "cancelled": true }`, `open-external` and `reveal` return `{ "ok": false }`,
and `window` returns `{ "ok": false }`.

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
| `navigator.getBattery` | optional; hide status when absent |
| `navigator.getGamepads` | Web Gamepad API always (host `onGamepad` is a no-op stub; capabilities report `"webkit"`) |
| `location.search` (token, deeplink) | unchanged; the host loads the same URL |
| `beforeunload` (shutdown) | page requests `/api/shutdown` when tracked games remain; the native host signals the Python child on window close — `SIGTERM` on Linux, `CTRL_BREAK` on Windows (see Server lifecycle) |

## Server lifecycle

- The host is the parent; it owns the Python child.
- On window close, the page requests `POST /api/shutdown` when tracked games
  remain, while the native host signals the Python child as the lifecycle
  owner: on Linux it sends `SIGTERM` to the child process group; on Windows
  (`native_host_win.c`) it sends `CTRL_BREAK` to the `CREATE_NEW_PROCESS_GROUP`
  child.
- `web_app.py` performs graceful teardown by stopping tracked sessions, waiting
  up to two seconds, force-killing known game process groups (or their PIDs),
  persisting final cleanup, and draining webhooks. The native host also waits
  for the child and, on Linux, sends `SIGKILL` to its process group if it remains
  alive; on Windows (`native_host_win.c`) the backstop is `TerminateJobObject`
  on the child's job object, which also kills children.
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
