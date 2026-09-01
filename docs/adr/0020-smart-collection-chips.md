# ADR 0020: Smart Collection Chips

**Date:** 2026-09-01
**Status:** Accepted

## Context

OpenBox's filter preset system stores collection rules as JSON in `pkg/parity/parity_filter_presets.py`. The UI rendered these as a flat list of rule descriptors, which was hard to scan and edit for complex collections with many rules.

Users needed a visual representation of filter rules that is both readable and round-trip faithful (editing a chip and saving must produce the same rule structure).

## Decision

Add **visual chip conversion helpers** in `pkg/parity/parity_filter_presets.py`:

1. `rules_to_chips(rules)` converts filter preset rules into UI chip descriptors (`{label, type, value, operator}`).
2. `chips_to_rules(chips)` converts chip descriptors back into filter preset rules.
3. The conversion is round-trip faithful: `chips_to_rules(rules_to_chips(rules))` equals `rules` for all valid rule sets.
4. Chips render as visual badges in the UI, each showing the field, operator, and value.
5. Existing filter rule normalization and persistence is preserved.

## Consequences

- Smart collections are easier to scan and edit visually.
- The JSON rule structure is unchanged; chips are a presentation layer.
- Round-trip fidelity is enforced by tests.
- No new dependencies; chips are plain Python dicts rendered as HTML.
