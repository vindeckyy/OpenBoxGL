# ADR 0074: Game Night decks and seeded draws

**Date:** 2026-10-18
**Status:** Accepted (1.17.0)

## Context

Game Night remembers the live queue in settings, but a host had to set players and the time budget again every
session. The 1.14.1 plan promised named deck presets and said the seed, not a materialised order, should travel.
The promise was deferred repeatedly.

## Decision

- A deck is `{name, query, players, minutes}` stored in `state["party_decks"]`, bounded to 20 decks, a 60-character
  name, and a 500-character query. The query is evaluated at build time through the canonical query pipeline, so a
  deck stores the question, not an answer.
- The seed is generated per draw, returned to the client, and not stored in the deck. A deck is reusable; one draw is
  reproducible.
- `build_party_queue(seed=...)` sorts eligible candidates by stable id before seeding `random.Random`, so the same
  library and seed produce the same order independent of the input list's order. Without a seed, behaviour is
  unchanged.
- The deck dialog loads a deck into the setup controls. Building sends `players`, `minutes`, `query`, and an optional
  8-character lowercase hex seed. The response echoes `seed`, `query`, and the filtered game count.
- Decks are local. Sharing the seed into Household and remote replay are deferred.

## Evidence

`tests/test_parity_party.py` covers bounds, upsert/delete, seeded reproducibility across input orders, and normalization.
`tests/test_handlers_party.py` covers query filtering, seed validation/echo, and the deck routes. The Big Box smoke test
is extended to save and reload a deck.

## Consequences

- The query grammar is permissive, so a syntactically odd filter may simply match nothing. Save-time validation still
  calls `parse_query`; a future stricter grammar will reject at save instead of silently changing a deck.
- `party_decks` is repaired on state load; no schema-version bump is needed.
