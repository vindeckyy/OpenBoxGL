# e03s04 — Root-cause and repair of confirmed defects

## 1. Identity

- **Story:** e03s04
- **Type:** repair
- **Risk:** P0
- **BCPs:** 8
- **Wave:** 3

## 2. User value

Bounded defects with reproducible mechanisms get smallest-root fixes and durable regressions; verified-mitigated items get regression coverage and are closed with evidence.

## 3. Context

Findings from e03s02/e03s03: I15 (.env read abort), I18 (settings snapshot outside final write lock), I14 (Gameyfin poll cap), I12 (favicon 404), plus I17/I16 verified as already mitigated by the flock/transaction path and copytree-before-backup ordering.

## 4. Problem statement

Each fix must be red-green where possible, and each candidate must reach a terminal disposition without destabilizing the product.

## 5. Purpose / callers / contracts

Production edits: `env_config.py`, `web_app.py`, `index.html`. Test edits: `test_env_config.py`, `test_bug_sweep_api.py`, `test_backend_hardening.py`, `test_plugins.py`. The candidate ledger records every finding.

## 6. Assumptions

- `STATE_LOCK` is a plain `threading.Lock`; the locked path must call `update_state_with_result`, never `transact_state` (re-entrancy deadlock).
- `index.html` may be edited only for the two targeted fixes (favicon link, poll cap); other hunks stay user-owned.

## 7. Exit criteria

Every accepted fix has a passing targeted regression; every candidate has a terminal disposition in the ledger.

## 13. Verification commands

```bash
python3 -B test_env_config.py
python3 -B test_bug_sweep_api.py
python3 -B test_backend_hardening.py
python3 -B test_plugins.py
```

## 17. Acceptance criteria

- [ ] I15: `.env` unreadable or binary files are skipped; startup never aborts.
- [ ] I18: settings snapshot, validation, and commit run under one state lock; concurrent distinct-key saves all persist.
- [ ] I17: update-vs-update across two store instances preserves both changes.
- [ ] I16: a swap failure after the old copy moved to `.backups` restores the previous version.
- [ ] I14: Gameyfin poll cap raised to 1200 attempts (30 minutes).
- [ ] I12: favicon routes serve the repo icon; no 404 on load.
- [ ] Ledger has a terminal disposition for every candidate.

## 18. Verification script (step-by-step)

1. Apply each fix with its regression in the same change.
2. Run the targeted test groups.
3. Confirm the I18 probe still shows no lost updates.
4. Record mechanisms and dispositions in the ledger.
