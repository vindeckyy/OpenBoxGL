# ADR 0075: Price paid and cost per hour

**Date:** 2026-10-18
**Status:** Accepted (1.17.0)

## Context

The backlog layer knows what a player did with a game, but not what it cost. Price paid and cost per hour had been
deferred since 1.14.1. A price is personal metadata; a wrong currency or a float rounding error would make the
totals quietly false.

## Decision

- Store `price_paid_cents` as an integer from 0 to 1,000,000,000 and `price_currency` as a three-letter uppercase
  ISO-4217-shaped code. Both absent means unset; `0` means free. Booleans and floats are refused.
- No conversion. Totals are grouped by currency and cost per hour uses the game's own currency. There is no
  exchange-rate source in the runtime, and inventing one would make historical prices depend on the day they are
  viewed.
- A rate exists only when the game cost money and has at least 60 seconds of playtime. Free and unplayed games still
  count toward their currency's total.
- The default currency is the `currency` setting, exposed in Settings > Library and validated on save. It is a default
  for new prices, not a conversion.
- Clearing a price removes both fields on the single-game and bulk paths. `_save_game_mutate` removes stale fields
  because `existing.update(game)` would otherwise leave an old price behind.
- Price stays out of the library-sync protocol in 1.17.0 for the same reason as ADR 0073: adding a catalog field makes
  a mixed-version peer reject the whole event. Time Machine therefore does not journal price changes yet.

## Evidence

`tests/test_catalog.py` covers cleaning, bulk set/clear, and the missing-currency refusal.
`tests/test_gapfill_rest.py` covers the single-game path and clearing both fields. `tests/test_insights.py` covers
per-currency totals, the one-minute threshold, and best/worst ordering. `tests/js/pure.test.mjs` covers currency
formatting and the invalid-code fallback.

## Consequences

- Cost per hour is a local convenience, not accounting. Multi-currency users see one line per currency.
- Syncing prices and journaling them in Time Machine is a future protocol change with its own ADR.
