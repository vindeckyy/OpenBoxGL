# ADR 0021: Hash Routing for Library View State

**Date:** 2026-09-02
**Status:** Accepted

## Context

OpenBox has no routing: refreshing the browser (or the native WebKitGTK host) always returns to the default library view, losing the active platform, playlist, filter preset, search query, selected game, and sort. The only URL surface is four one-shot `?deeplink=` query modes read once at boot, which exist for `openbox://` deep links and must keep working unchanged.

The app is a single persistent Library view with dialog overlays, not a multi-page SPA, so a full router framework is unnecessary; only *view context* needs to survive a reload.

## Decision

Encode the library view state in the URL hash via a new `static/router.js` module:

1. **Grammar:** `#/key/value` pairs joined by `/`, in a fixed key order: `platform`, `category`, `playlist`, `preset`, `q`, `game`, `sort`. Values are `encodeURIComponent`-encoded; empty/default values are omitted. Example: `#/platform/SNES/q/mario/game/12`.
2. **Write path:** `syncHash()` is called at the end of `render()` and `renderGrid()`, and uses `history.replaceState` only — no history entries, no `hashchange` feedback loop.
3. **Apply path:** `applyHash()` parses the hash into `AppState` and toolbar inputs, and returns whether anything changed. It runs once before the first render, and on `hashchange` (user edits the URL, shared links) followed by `render()`.
4. **Out of scope on purpose:** the grid/list toggle stays a persisted setting (`library_view`), not a route value; the `?deeplink=` query modes are untouched and keep precedence for their one-shot behavior.

## Consequences

- Refreshing restores platform/playlist/preset/query/selection/sort; URLs are bookmarkable and shareable.
- No history spam: Back exits the app rather than stepping through view states — deliberate, since view transitions are not navigation in the user's mental model.
- The hash is presentation state only; nothing server-side parses it, so the v1/v2 route contracts are unaffected.
- Adding a new routed key means appending to `ROUTE_KEYS` order and one `pair()` in `applyHash()` plus one `push()` in `currentHash()`.
