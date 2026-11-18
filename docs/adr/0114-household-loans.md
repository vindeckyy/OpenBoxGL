# ADR 0114: Household loans deferred to 2.3

Status: Deferred to 2.3 (maintainer decision 2026-11-07)

## Context

The 2.2 proposal included a Loan Board (W6): physical-game lending tracked through household sync, with a new `loan` record kind and a household sync format 1→2 fail-closed bump. On 2026-11-07 the maintainer deferred it to 2.3 so 2.2 ships five pillars. This note reserves the number and records the decision; it is not an adopted design.

## Decision

- Loan Board does not ship in 2.2.0. No `parity_loans.py`, no `loans` state key, no loan routes, no loan UI.
- Household sync format stays **1** in 2.2. No format bump, no mixed-version household warning, no fail-closed change.
- The full design — the `loan` record kind, the format-2 fail-closed bump, routes, overdue sweep, and tests — remains in `docs/NEXT_UPDATE_PLAN-2.2.md` §12 as the 2.3 planning input.

## Consequences

- 2.2 changes no sync format; mixed-version households are unaffected by this release.
- 2.3 planning starts from a written design rather than a blank page; the format-2 bump and its old-peer rejection test move with it.

## Verification

`docs/NEXT_UPDATE_PLAN-2.2.md` §12 (deferred design); `pkg/parity/parity_household.py` keeps `HOUSEHOLD_FORMAT = 1` through 2.2.