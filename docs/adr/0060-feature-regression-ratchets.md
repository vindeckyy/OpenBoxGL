# ADR 0060: Feature-regression ratchets

**Date:** 2026-09-26
**Status:** Accepted (1.14.1)

## Context

The gate suite proved a great deal about *code*. Ruff caught unused imports,
coverage floors caught untested lines, the v1 contract froze the 61 routes the
native host depends on, and the token/i18n/CSP checks held their lines.

It proved almost nothing about *features*. A feature here is a route, a
settings key, an emulator definition, a frontend module, or a documented
failure mode — and every one could vanish in a commit that passed every
existing check:

- **213 of 274 routes were unprotected.** `v1_contracts.json` freezes the v1
  surface. The 111 `/api/v2/*` routes added since 1.9 and 208 unversioned
  routes had no contract. Deleting one turns a feature into a 404 that only
  appears in a browser, on a user's machine.
- **`KNOWN_SETTINGS` is a destructive allowlist.** `sanitize_settings` and
  `prune_unknown_settings` *delete* any key not in the set. Removing one line
  erases that setting from every existing library on the next save, and no test
  fails, because the write path is behaving exactly as written.
- **ADR 0048's `native_exe_windows` was unenforced.** All 24 definitions carry
  it, but nothing required it. A 25th without it would launch on Linux and fail
  on Windows — the exact regression class 1.13 removed.
- **The frontend module graph was unverified.** A dangling import breaks the
  page at runtime; an orphaned module leaves a feature silently unreachable
  while the file still ships.
- **`docs/reliability.md` claimed coverage by naming test files.** Nothing
  checked those files still existed, so a rename turned 23 rows into fiction.
- **The What's New dialog lied for two releases.** Every locale hardcoded
  `"...in OpenBox 1.11"`, and `check_version_sync.py` never read the locale
  files, so the drift was invisible to the gate by construction.

Commit `71d75ec` is the cautionary tale: a test pinned `NOW = 2026-09-12`
against a handler reading `datetime.now()` with `RADIO_STALE_DAYS = 7`. It
passed for a week, then would have failed the release gate forever. The tree
holds ten such window constants.

## Decision

Six gates, each a **ratchet** stored in `scripts/contracts/`, all run by
`scripts/check_tests.py` (Stage 2.9–2.14) and by both the Linux `gate` and
`windows-latest` CI jobs.

1. **`check_routes_contract.py`** — the full route surface, not just v1. A
   baselined route that disappears, is renamed, or changes method fails.
2. **`check_settings_contract.py`** — the `KNOWN_SETTINGS` allowlist. Retiring a
   key holding persisted user data additionally requires a reason naming the
   migration that preserves existing values.
3. **`check_emulator_defs.py`** — every definition must carry the required keys
   including `native_exe_windows`, and the *set* is ratcheted.
4. **`check_frontend_modules.py`** — resolves every relative import against the
   filesystem, flags dangling imports and orphans, ratchets the shipped set.
5. **`check_reliability_catalog.py`** — every `Tested` row must name a test
   that exists (`test_*.py` or the `ui_smoke` harness), rows stay contiguous,
   and a failure mode cannot be dropped.
6. **`check_clock_coupling.py`** — a test pinning a calendar constant while
   exercising a rolling `*_DAYS` window must carry a `# clock-coupled:` marker
   and a recorded exemption.


### The two-layer rule

A one-layer ratchet is trivially defeated by editing its own baseline, so every
contract gate is ratcheted in two layers:

- **Layer 1** compares live code against the committed baseline.
- **Layer 2** compares the baseline against the copy in `git HEAD`. Deleting an
  entry to hide a removal is itself a failure.

A surface may only shrink by moving the entry into a `retired` ledger with a
non-empty reason. The ledger is itself append-only: entries cannot be deleted
or reworded, cannot be fabricated for something that never existed, and cannot
duplicate a live route. This is the key property — **removal is possible but
never silent, and always leaves a permanent, reviewable record.**

Where the runtime prunes on save (settings), a retired reason must also name
the data-preservation migration.

### Asymmetry

Every gate permits **growth** and restricts **shrinkage**. Adding a route, key,
definition, module, or reliability row is free and expected; the gate prints
the delta and points at `--update`. Removing one is a deliberate act. This is
what lets the suite stay useful while a feature-dense project keeps shipping
features.

### Calibration

Baselines are generated from the tree with `--update` (`make contracts`). The
git layer is skipped with a printed notice when git is unavailable (source
tarball, container copy) rather than failing, because Layer 1 still protects
the shipped artifact.

Two real defects were found while calibrating, and both are fixed:

- The What's New title hardcoded a version in all five locales. It is now a
  `{version}` template filled from the live settings payload, and
  `check_version_sync.py` fails on any `OpenBox 1.x` literal in a locale.
- `test_parity_radio.py` was genuinely clock-coupled; it now carries a
  `# clock-coupled:` marker recording that every call injects `now=NOW`.

## Consequences

- Six new gate scripts and six frozen contract files under
  `scripts/contracts/`. They are dev-only and deliberately absent from
  `runtime_modules.txt`, like `run_windows_tests.py`.
- `tests/test_feature_contracts.py` covers the gates, including that each
  contract file exists, parses, and stays wired into both the orchestrator and
  CI — so a gate cannot be quietly unhooked.
- Coverage floors are unaffected; these checks run before the test stage.
- The ratchets cost a few seconds and no network. They run identically on
  Linux and Windows, so the `windows-latest` job proves they are not
  POSIX-only.
- A future removal is now a diff line in a JSON ledger with a reason, reviewed
  in the same PR. That is the intended cost: features may be retired, but not
  by accident and not without a record of what was lost and why.

