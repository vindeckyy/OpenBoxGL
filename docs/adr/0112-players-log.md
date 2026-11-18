# ADR 0112: Player's Log — Time Capsules and Milestone Cards

Status: Accepted (2026-11-17)

## Context

Notes are dated (ADR 0058) but nothing hides content until a future date, and while Wrapped and Play Insights aggregate the long arc, nothing emits discrete shareable milestone events. The 2.2 plan (`docs/NEXT_UPDATE_PLAN-2.2.md` §10) adds both as pure functions of existing state: no new data collection, no network.

## Decision

- **Time Capsules.** `state["time_capsules"]` (additive array, default `[]`): `{id, game_id?, title≤120, body≤4000, kind: note|question, unlock_at, created_at, opened_at?}`. `unlock_at` is a local date (YYYY-MM-DD) that unlocks at local midnight, never earlier; all date logic takes an injected `today`. `game_id` is a label, not a foreign key — deleting a game keeps its capsules.
- **Server-side redaction.** `body` is literally absent from every list/detail payload until `opened_at` is set — not merely hidden client-side. `POST /api/v2/capsules/open` sets `opened_at` once (idempotent) and returns the body.
- **Exactly-once unlock.** A sweep on server startup (`main()`) plus the hourly tick sets `opened_at` and emits one notification (`kind:"capsule"`, `dedupe_key:"cap:{id}"`, keys registered in `SERVER_NOTICE_KEYS`) for each capsule with `unlock_at <= today` and `opened_at == null`. Exactly-once is guaranteed by the durable `opened_at` field, **not** the notification dedupe key — the feed's `CAP=200` can evict keys. A clock moved backward after open never re-locks or duplicates.
- **Milestone Cards.** Milestones are derived, never stored (only `ui_state.milestones_seen` for "new" badges). IDs are deterministic; the catalog covers per-game/total playtime, first clear, sessions, RA points/completion, streaks, library age, and collection size. `unlocked_at` is derived by binary-search over session history for the threshold-crossing date, with an `approximate:true` fallback to `last_played` where history is insufficient. RA kinds degrade when the cache is absent; streaks reuse `compute_streak` (`pkg/parity/parity_insights.py`).
- **Card rendering.** The story-card renderer `buildShareCard` (`static/moments.js`) is factored into a shared renderer that Milestone Cards reuse: client-side 1200×630 canvas → PNG blob → download (with `toDataURL` fallback), theme-token colors, no external assets, works offline, `prefers-reduced-motion` respected.
- Routes: `GET/POST /api/v2/capsules`, `POST /api/v2/capsules/open` (idempotent `200` once due; `409 CAPSULE_LOCKED` before `unlock_at`), `POST /api/v2/capsules/delete`, `GET /api/v2/capsules/next`, `GET /api/v2/insights/milestones`. Codes: `CAPSULE_NOT_FOUND`, `CAPSULE_LOCKED`, `CAPSULE_INVALID_DATE`, `CAPSULE_BODY_TOO_LARGE`.
- **Honesty.** Sealed ≠ encrypted: capsule bytes sit in `library.json` like everything else; "sealed" means the app won't show the body early, not that it is unreadable. Feature docs and the upgrade guide say this plainly. No scheduled delivery, email, or push — unlock surfaces as an in-app notification.

## Consequences

- The same library always yields the same milestone set; deleting `ui_state.milestones_seen` costs only the "new" badges.
- Capsules and milestones add no network surface and no new secret storage.
- Date tests use an injected clock to avoid clock-coupling ledger entries; a marker + `scripts/contracts/clock_coupling.json` entry is added only where a rolling-window pin proves unavoidable.

## Verification

Planned: `tests/test_playerlog.py` (CRUD/bounds, DST/month edges, redaction, exactly-once, idempotent open, delete-while-locked, game deletion keeps capsules), `tests/test_playerlog_http.py`, `tests/test_playerlog_milestones.py` (threshold boundaries, crossing-date derivation, determinism, RA-absent), and js tests for the factored card-layout helper. Acceptance: plan §6-W4.