# ADR 0050: Household presence transport

**Date:** 2026-09-16
**Status:** Accepted

## Context

Household 2.0 needs a "who is playing what" answer without a server and without
a durable activity history. The existing Household transport already exchanges
immutable, content-addressed records through a user-mounted folder
(`openbox-household-v1/`, ADR 0039 vocabulary). Presence must be opt-in per
member, off by default, and must never carry paths, launch configuration, or
credentials.

## Decision

- `pkg/parity/parity_presence.py` defines a heartbeat record (`format` 1,
  `kind: heartbeat`) signed with the same content hash used for household
  records: `event_id = sha256(canonical(body))`. Readers recompute the hash and
  drop anything that does not match.
- Heartbeats live in `openbox-household-v1/presence/` as one slot file per
  `(device_id, member_id)`, named by a truncated SHA-256 of both. Writing
  replaces the slot, so the transport retains no history by construction.
- `HEARTBEAT_TTL_SECONDS` is 600. `validate_heartbeat()` rejects a TTL outside
  30–600s; `heartbeat_expired()` treats any invalid record as expired; reads
  prune expired slot files. Projection only emits unexpired events.
- Opt-in lives in `state["household"]["presence"]` (`opted_in` map + `toast`
  flag). It is off by default, per member, and local-only: the heartbeat route
  refuses members who did not opt in.
- Routes: `POST /api/v2/household/presence` toggles the opt-in,
  `POST /api/v2/household/presence/heartbeat` publishes one beat, and
  `GET /api/v2/household/activity` returns the projection (member, game label,
  platform, elapsed seconds, and a `game-night` join hint when the sender is in
  a party queue). All are `/api/v2`-only and the v1 surface is untouched.
- `static/household.js` renders a live strip and an optional rate-limited
  toast (one per member per TTL). Heartbeats are best-effort and never surface
  failures to the user.

## Consequences

- Presence is a projection, not a record: there is no retained activity
  history to review, export, or leak.
- A missing shared folder degrades honestly (`available: false`), and a
  tampered file is ignored rather than trusted.
- Members must be added locally before they can opt in; the opt-in stays on
  the device that owns the member identity.
