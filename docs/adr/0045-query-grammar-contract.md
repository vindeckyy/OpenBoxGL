# ADR 0045: Deterministic natural-language query grammar

**Date:** 2026-09-12
**Status:** Accepted

## Context

The library search bar needs a compact way to express common filters such as
`short unplayed rpg`, `couch co-op for 4`, and `90s platformers rated 4+`.
Interpretation must be explainable, editable as chips, and stable across
devices; an opaque or probabilistic parser could silently broaden a filter.

## Decision

- `pkg/parity/parity_query.py` uses a deterministic pipeline:
  tokenizer → longest-match grammar AST → compiled preset rules and extended
  clauses → ADR-0020 chip descriptors.
- Recognized constraints are represented by the existing preset-rule
  vocabulary where possible. Alias families such as genres use explicit
  `genre_any` clauses when one phrase expands to multiple safe terms; every
  emitted rule or clause has a corresponding chip.
- Unknown words remain a plain substring query and the response includes a
  hint. If nothing matches the grammar, `parsed` is false; the parser never
  guesses a filter from an unrecognized phrase.
- `POST /api/v2/library/query/parse` is a bounded, read-only preview. It
  returns the text, parse status, rules, clauses, chips, leftovers, hint, and
  a match count without mutating library state.
- The matcher evaluates both the shared preset rules and every extended
  clause, failing closed for unknown clause kinds.

## Consequences

The same query has the same interpretation locally and in tests, and the UI
can let users remove or edit individual chips. New grammar phrases require
fixture coverage and chip semantics; no model, network service, or hidden
ranking step is part of the contract.
