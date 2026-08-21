# Native host contract

The native host renders the same `index.html`, `app.js`, and `app.css` as the
web UI, served by `web_app.py` over loopback. This document is the contract
between the page and the host. Both directions must keep working when the host
is absent, so the web UI remains usable in a plain browser for development.

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

  // Host feeds gamepad state as events instead of the Web Gamepad API.
  onGamepad(callback) -> void,
};
```

The bridge is a direct path; the HTTP surface below is the fallback that works
even when bridge injection is unavailable, and it is what the tests exercise.

## HTTP native surface

All native routes are authenticated like every other route (and return capability fallbacks when the native host is absent).

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
  "gamepad": "sdl" | "webkit" | "none",
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
| `prompt(...)` (legacy; now styled in-page dialog for import paths, playlist names, etc. — previously 16 call sites across the 13-module frontend) | `dialog("file"/"folder")` or styled in-page prompt |
| `confirm(...)` (legacy; now styled in-page confirm for deletes/restores — previously 13 sites) | styled in-page confirm or native dialog |
| `localStorage` (UI prefs) | server-persisted `ui_state` when native; `localStorage` in browser |
| `navigator.clipboard` | host clipboard; browser fallback |
| `document.fullscreenElement` / request/exit | `windowAction("set-fullscreen"/"unset-fullscreen")` |
| `navigator.getBattery` | optional; hide status when absent |
| `navigator.getGamepads` | `onGamepad` events when host reports `sdl` |
| `location.search` (token, deeplink) | unchanged; the host loads the same URL |
| `beforeunload` (shutdown) | host calls `/api/shutdown` on window close |

## Server lifecycle

- The host is the parent; it owns the Python child.
- On window close, the host calls `POST /api/shutdown` (the existing graceful
  path that stops sessions and drains webhooks), then SIGTERMs the child as a
  backstop.
- The host holds no credentials. The token stays in the URL and the
  owner-readable `server.token` file, unchanged from the current threat model.

## Persistence

Window geometry and the last session live in `ui_state`, not `settings`:

```json
{
  "ui_state": {
    "window": { "x": 0, "y": 0, "w": 1280, "h": 780, "maximized": false },
    "last_session": { "view": "grid", "selected_id": "", "platform": "all", "playlist": "" },
    "single_instance": true
  }
}
```

On Wayland, absolute window position is WM-controlled and not persisted; the
host persists size and maximized state only.
