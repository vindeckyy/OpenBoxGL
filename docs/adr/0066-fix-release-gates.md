# ADR 0066: Gates added in the 1.16.1 fix release

**Date:** 2026-10-08
**Status:** Accepted (1.16.1)

## Context

The 1.16.0 sweep and this audit found the same defect classes repeating: a probe
without a timeout, a game addressed by its array index, a timestamp that a naive
comparison silently drops, and an English string shown to users of four other
locales. Each had a fix, and none had a gate, so each could return.

## Decision

Add these gates. Each is a ratchet: it records a measured allowance that must match
the code exactly, so a new occurrence fails the gate and so does a fix that leaves
the allowance too high. Lowering an allowance happens in the same change as the fix.

| Gate | What it counts | Allowance | Where |
|---|---|---|---|
| Subprocess timeout | `subprocess.run/call/check_output/check_call` without `timeout=` in runtime modules | `updates.py`: 1 (a long Windows update verification) | `tests/test_subprocess_timeouts.py` |
| Game addressing (frontend) | Game-addressed API requests that send only the array index | 0 | `tests/test_game_addressing_ratchet.py` |
| Timestamp parsing | Bare `datetime.fromisoformat` outside `pkg/platform_compat.py` | 29 | `tests/test_game_addressing_ratchet.py` |
| English notify | Literal English `notify()` calls in `static/*.js` | 137 | `tests/test_notify_literals.py` |
| Stray files | Untracked entries at the repository root that a test run creates | new entries fail | `scripts/check_tests.py` (`_root_untracked_entries`) |

The gates live in the test suite rather than in `scripts/contracts/`, because they
are small static scans and the allowance is the whole contract. The stray-file check
is the one exception to the test-file pattern; it runs inside the full gate because
it needs the suite to have run.

## Consequences

- A fix that removes a call site must lower its allowance in the same change. The
  gate says so when the allowance is too high.
- The frontend gates are pattern scans, not parsers. They can miss a call written in
  an unusual shape and can count a non-game id; the counts are a floor on the
  problem, not proof that it is gone.
- The `datetime.fromisoformat` and English-notify allowances are large (29 and 137).
  They are ratchets to be walked down, not targets to keep.
