# ADR 0051: Party deck sharing

**Date:** 2026-09-16
**Status:** Accepted

## Context

Game Night can build a queue (ADR 0031) but cannot save, reload, theme, or
share one. A shared queue must reproduce the same order on another device
without exchanging anything except a small JSON payload — path and launch data
stay local per ADR 0039.

## Decision

- `pkg/parity/parity_party.py` gains three additive pieces:
  1. **Theme presets** (`THEME_PRESETS`): `nineties_racers` (1990–1999 year plus
     racing keywords in name/genre/tags), `coop_only` (explicit co-op evidence
     or controller support), `eight_plus` (max_players >= 8). Presets filter the
     already couch-eligible candidate list and never invent eligibility.
  2. **Deterministic seeded shuffle**: `seeded_shuffle(ids, seed)` uses
     `random.Random("openbox-party-v1:<seed>")`, so the same seed and ids always
     reproduce one order. `build_party_queue(..., seed=)` returns that payload
     order; without a seed the historical random tiebreak is preserved.
  3. **Deck storage**: a deck is `{format, deck_id, name, players, minutes,
     preset, seed, game_ids, created_at, signature}` with the signature hashing
     the shareable fields. Decks persist in `state["party_decks"]`.
- `deck_queue(deck)` is the reproducibility contract: `game_ids` carries the
  eligible selection and `seed` alone determines the order, so a shared deck
  reproduces order even when ratings differ on the receiving device.
- Routes under `/api/v2`: list/save (`/party/decks`), load, delete, share, and
  import. Loading a deck writes the resolved order into the existing
  `party_queue`/`party_players`/`party_index` settings; no new settings keys.
- Sharing reuses the Household transport namespace: decks are written
  content-addressed to `openbox-household-v1/party-decks/<signature>.json`.
  Import validates the signature, skips decks whose id or name already exists,
  and never overwrites a local deck.
- `static/party.js` exposes the builder in the setup card: preset select, deck
  name, save, load, share, delete, and import controls.

## Consequences

- A shared deck is tiny metadata (no paths, media, or stats) and works through
  any folder transport the household already uses.
- Two devices can share a seed without sharing the same library: the payload
  carries the chosen game ids, so order reproduction does not depend on local
  ratings.
- Presets are evidence-based filters, not a genre taxonomy; an empty preset
  result is surfaced with an explicit message instead of a generic empty queue.
