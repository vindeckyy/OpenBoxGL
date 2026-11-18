# ADR 0111: Deal Radar — opt-in Steam price watch

Status: Accepted (2026-11-17)

## Context

`price_paid_cents`/`price_currency` record what a game cost (ADR 0075), but nothing watches current prices or alerts on drops, and the library has no place to track games not yet bought. Deal Radar is the only 2.2 feature that talks to a storefront, so it must be opt-in, bounded, and explicit about exactly what leaves the machine. Design: `docs/NEXT_UPDATE_PLAN-2.2.md` §9.

## Decision

- `state["deals"] = {watchlist, prices, last_poll_at}` (additive, schema stays 7). Bounds: watchlist ≤200 entries, price history ≤50 points per entry (oldest dropped), `note` ≤280 chars, `target_price_cents` ≥0 or null. Malformed entries are dropped by warn-and-repair, never refusing the library.
- The fetch helper follows the `github_request` conventions (injectable opener, `read_limited` response cap, timeout 15 s, UA `OpenBox/{VERSION}`) with a hard host allowlist of exactly `store.steampowered.com` enforced on every request and HTTP redirect hop; appid validated `^\d{1,10}$`; no user-supplied URLs. Endpoints: `storesearch` (sanitized query, ≤10 results) and `appdetails?filters=name,price_overview,header_image` (missing appid ⇒ `not_found`, not an error).
- Off by default: `deals_enabled=false` ⇒ zero outbound requests and every route returns `available:false` (or refuses mutations/fetches with `409 DEALS_DISABLED`). The enable flow shows a one-line confirmation naming the host before activation.
- Weekly default poll: `deals_poll_hours` 168 (clamp 24–720), `deals_min_drop_pct` 10 (clamp 1–90), `deals_store_cc` `us`. The scheduler piggybacks the hourly tick: enabled and `now − last_poll_at ≥ deals_poll_hours` ⇒ submit the `deals_refresh` job — sequential, ≥1 s spacing, per-entry error isolation, cancellable between entries. Unwatching removes both the watchlist entry and its `prices[id]` history by default so `library.json` stays bounded.
- Drop/target notifications fire when `discount_pct ≥ deals_min_drop_pct` and price < previous, or price ≤ target. Dedupe is `deal:{id}:{price_cents}` **plus a durable `last_notified_price` marker on the entry**: the notification feed's `CAP=200` can evict dedupe keys, so the durable marker — not the feed — guarantees one notification per price point. The first poll of an entry seeds history silently.
- Manual entries (`manual:true`, `appid:null`) never poll; users log price observations for them via the entry route (`source:"manual"`).
- No currency conversion: the currency string is stored per price point and passed through, consistent with ADR 0075. `cc` is a display-currency parameter only.
- Response cache `cache/deals/<appid>.json` with TTL = poll interval, so UI reads never hit the network. Routes: `GET /api/v2/deals`, `GET /api/v2/deals/search`, `POST /api/v2/deals/watch`, `POST /api/v2/deals/unwatch`, `POST /api/v2/deals/entry`, `POST /api/v2/deals/refresh`. Codes: `DEALS_DISABLED`, `DEALS_INVALID_APPID`, `DEALS_NOT_FOUND`, `DEALS_LIMIT`.

## Consequences

- The only bytes that leave the machine are appids and search queries to `store.steampowered.com` — no cookies, no account, no telemetry. The compat report and privacy docs name the exact host and payload.
- History is bounded, so "lowest ever" means lowest within the retained points; the UI labels stale prices with their age.
- Storefront drift degrades the feature (cached prices + manual entries keep working), never breaks it.

## Verification

Planned: `tests/test_deals.py` (CRUD/bounds, appdetails shapes, history cap, both drop branches, first-poll silent, dedupe + durable marker, manual never polled, currency passthrough), `tests/test_deals_http.py`, `tests/test_deals_job.py` (cancel mid-run, per-entry isolation, politeness delay), `tests/test_deals_privacy.py` (allowlist rejects every other host; zero socket calls when disabled). Spike W0-03 records sanitized endpoint samples here, or a fixture fallback if the network spike is skipped. Acceptance: plan §6-W3.