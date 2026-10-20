# ADR 0083: Native tray recents, launch deeplink, and battery capability

**Date:** 2026-10-19
**Status:** Accepted (1.18.0)

## Context

The native host owns the tray menu and the window; the web UI owns the library and the launch path. Two host features
needed a contract between them:

- The tray should launch a recent game directly. Reading `library.json` from the C host would couple the host to the
  state schema and duplicate history/dedupe logic, and an unauthenticated URL surface would let any local page start a
  game.
- Big Box on a handheld should show battery state. The Web Battery API is unavailable in some WebViews, so Linux needs
  a sysfs bridge that does not expose sysfs paths to the page.

`docs/native-host-contract.md` is the published contract for host IPC, routes, the recent-games file, deeplinks, and
capability fallbacks. This ADR records why that shape was chosen.

## Decision

### Recent-games projection

- `write_recent_games` (`pkg/parity/parity_tray_recent.py`) publishes `<data>/native-host-recent` through
  `secure_text_write` (atomic, mode `0600`). It is a projection, not state: each line is `display name<TAB>game id`.
- The list holds at most eight games (`RECENT_LIMIT`), most recent first, unique by `game_id`. Session history is read
  newest-first. Games with a `last_played` value fill the remaining slots, newest first. A missing or malformed history
  is ignored.
- Names have tabs and whitespace runs collapsed, are capped at 120 characters, and fall back to `Untitled`. The hosts
  split each line at the first tab.
- The file is written at boot (`web_app.py`) and after each session commit (`pkg/state/launch.py`). Write errors
  (`OSError`, `ValueError`) are logged and do not block boot or a session commit.

### Tray

- Both hosts create the tray icon only when `tray_enabled` is set in `<data>/native-host-flags`, which `web_app.py`
  writes at boot.
- The menu is rebuilt when it opens and reads the recent-games file. It does not query the library.
- Linux (`native_host.c`, `rebuild_tray_menu`): "Show OpenBox", a separator, a disabled "Recent games" header, up to
  eight game items, or a disabled "No recent games" item when the file is missing or has no valid lines.
- Windows (`native_host_win.c`, `show_tray_menu`, right-click): "Show OpenBox", a "Recent games" submenu only when at
  least one entry parses, a separator, and "Quit".

### Launch deeplink

- Linux tray items load `/?token=<session token>&deeplink=launch&id=<URI-escaped game id>` in the native window
  (`on_tray_recent`). This is the only path that sends `deeplink=launch` to the page.
- Windows tray items, and `openbox://launch/<id>` on either host, go through the host. It POSTs `/api/launch` and then
  loads `/?token=<session token>&deeplink=showgame&id=<URI-escaped game id>`. The URI reaches the host as an argument or
  through the single-instance channel, which forwards it to the running window. The installer registers the
  `openbox://` protocol on Windows (`scripts/install.ps1`).
- `openbox --play <id>` and `openbox --uri <uri>` (`parity_deeplinks.dispatch_uri`) POST `/api/launch` without opening
  a window.
- The browser dispatches `?deeplink=` through the `DEEPLINK_ACTIONS` map in `static/app.js`: `bigbox`, `settings`,
  `showgame`, `launch`, `recap`, `moment`, and `clip`. `search` is read separately to seed the search box. Any other
  value is ignored.
- `launch` resolves the id with `resolveDeeplinkGameId` (`static/state.js`): a stable `game_id` first, then a numeric
  library index. It calls `launch()` (`static/sessions.js`), which posts to `/api/launch`, so the normal launch path runs.
- No new HTTP route was added for launching.

### Battery capability

- `GET /api/native/battery` (`handlers/native.py`, `battery`) requires page authorization and returns
  `{present, percent, charging}`.
- Linux: reads the first `BAT*` entry in name order under `/sys/class/power_supply` whose `capacity` parses as an
  integer. The capacity is clamped to 0-100, and `charging` is true for `Charging` and `Full`. The sysfs path is never
  returned. Other platforms return `present: false` without reading anything.
- The page (`readBattery` in `static/battery.js`) uses `navigator.getBattery` when the browser provides it, and
  otherwise calls the route. Failures become absent. The result is cached in the page for 60 seconds. Big Box reads it
  on each open (`static/bigbox.js`).
- `battery_indicator` (default `true`, saved by `handlers/settings.py`) hides the Big Box indicator (`#bigBoxBattery`)
  when false. An absent reading renders nothing.
- `tray` in `GET /api/native/capabilities` is true only when `OPENBOX_NATIVE_HOST` is set. Both hosts set it.

## Alternatives considered

- **Let the host parse `library.json`.** Rejected: it couples the native binaries to a state schema that changes
  independently, and duplicates history ordering and name sanitization in two languages.
- **Have Python write a host-specific menu model.** Rejected: GTK and Win32 menus differ; the shared contract is the
  small tab-separated file, and each host builds its own menu.
- **Launch by array index from the host.** Rejected: indices shift when the library changes; a stable `game_id` is
  already the cross-process identity. The numeric index fallback remains for callers that pass an index.
- **Execute an arbitrary `deeplink` value.** Rejected: a query parameter is untrusted input; an allowlist keeps the
  surface to named UI actions.
- **Add a native battery call.** Rejected: it would duplicate platform code and would not work in a plain browser; the
  HTTP route with a browser-API preference serves both.
- **Write the recent list into library state.** Rejected: it is derived data; persisting it would create a second
  source of truth for recency.

## Evidence

`tests/test_parity_tray_recent.py` covers history order and uniqueness, the `last_played` fallback, single-line names,
owner-only tab-separated output, and the eight-game limit. `tests/test_native_handlers.py` covers the battery sysfs
reader (charging, clamping, missing root) and route authorization; no test covers the non-Linux response.
`tests/test_frontend_contract.py` covers the deeplink allowlist. `tests/test_native_host.py` covers the host's
authenticated deeplink construction and single-instance forwarding.

## Consequences

- The tray list is a snapshot refreshed at boot and after each session commit. Opening the menu reads the file, not
  the library.
- The file is derived and disposable. Deleting it shows "No recent games" on Linux and hides the submenu on Windows
  until the next write.
- Deeplink growth requires adding a named action to the allowlist; there is no generic escape hatch.
- Battery state is advisory. A desktop or an unreadable sysfs tree reports absent and hides the indicator.
