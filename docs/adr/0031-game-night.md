# ADR 0031: Game Night Big Box party mode

**Date:** 2026-09-04
**Status:** Accepted

## Context

Couch multiplayer needs a zero-setup flow: pick player count and session length, get a fair queue, spin for the first game, launch, advance rounds. All data already exists in `library.json` (`max_players`, `controller_support`, `platform`, `rating`); no new persistence beyond the queue itself is required.

## Decision

1. **Pure queue builder**: `pkg/parity/parity_party.py` exposes `build_party_queue(games, *, players=2, minutes=0, limit=50)`. Eligibility = visible, not `hide_in_bigbox`, path usable (same default as the picker), `max_players >= players`, and couch-suitable (platform in the static `COUCH_PLATFORMS` set — consoles, handhelds, arcade — or explicit per-game `controller_support`). A `minutes` budget excludes titles averaging more than 3x the session length (same factor as the picker). Sort is rating desc with a random tiebreak; output is capped at 50 game ids.
2. **Three additive routes** in `handlers/party.py` (`PartyHandlers` mixin):
   - `POST /api/v2/party/queue` `{players, minutes}` builds via (1) and persists `{party_queue, party_players, party_index: 0}` through `transact_state`.
   - `GET /api/v2/party/queue` returns `{queue, index}` from settings (defensively re-cleaned).
   - `POST /api/v2/party/next` advances the persisted index with wrap-around and resolves the game name, returning `{game_id, name, index}`. Launch stays client-side via the existing `/api/launch` path.
3. **Settings**: `party_queue` (deduped, ≤50 game-id strings), `party_players` (int 2–8), `party_index` (int ≥0, clamped) in `KNOWN_SETTINGS` with a `_clean_party()` validator, so the queue survives restarts (consistent with ADR 0011).
4. **POST handler contract fix**: `_do_POST` reads `rfile` exactly once and passes the parsed dict to the handler. `handlers/picker.py` (M2) called `self.body()` a second time, which blocks on the exhausted keep-alive stream until the socket times out — POST `/api/v2/library/pick` hung live while passing unit tests. Fixed both picker and party handlers to take the `payload` argument; handler tests now pass parsed dicts.
5. **Frontend**: `static/party.js` renders `#partyOverlay` inside the Big Box section (setup card → wheel → launch/next-round). The wheel is a `conic-gradient` div spun with a 2.4 s `cubic-bezier(0.2, 0.8, 0.2, 1)` rotation landing the `queue[index]` segment under a top pointer; "Up next" shows the next 3 covers. Gamepad input reuses `pollGamepads()` edge detection via a `partyGamepad(edge)` branch (left/right adjust setup, play = build/spin/launch, up = next round, back = close, menu = Big Box menu). Keyboard fallback (arrows/Enter/N/Escape) is verified in `ui_smoke`; the overlay captures keys so Big Box navigation does not fire underneath. No new CSS tokens — the wheel reuses `--brand/--accent/--active/--focus`.
6. **Big Box menu**: a "Game Night" button in `#bigBoxMenu` opens the overlay (reuses the `menu` controller action; no new `controller_map` entry per the plan's default).

## Consequences

- One unpaired state shape: `party_index` persists alongside the queue so a restart resumes the round order.
- Steam Deck manual pass: pending maintainer verification; gamepad path shares the exercised `pollGamepads` edge pipeline and the CI keyboard path covers the same state machine.
