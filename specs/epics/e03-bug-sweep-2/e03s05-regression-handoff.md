# e03s05 — Whole-product regression, packaging, and sweep handoff

## 1. Identity

- **Story:** e03s05
- **Type:** regression + handoff
- **Risk:** P1
- **BCPs:** 3
- **Wave:** 4

## 2. User value

The product leaves the sweep green end-to-end: full suite, rebuilt AppImage with packaging validation, reconciled backlog/state, and a consistency-passing capsule.

## 3. Context

All e03s04 repairs are in. Packaged content changed (`web_app.py`, `env_config.py`, `index.html`), so the AppImage must be rebuilt and revalidated per repo policy.

## 4. Problem statement

Without the final gates, a fix could regress an adjacent subsystem or the sweep could leave stale issue/state records.

## 5. Purpose / callers / contracts

Writes `specs/verifications/e03s05-final.md`; reconciles `.agent/ISSUES.md`, `.agent/STATE.md`, and `.agent/journal/2026-08.md`; runs the capsule consistency checker.

## 6. Assumptions

- AppImage and zsync artifacts are gitignored by policy; CI publishes them at release.
- `OPENBOX_REQUIRE_ARTIFACTS=1` forces full AppImage structure validation.

## 7. Exit criteria

Full suite exit 0; packaging tests exit 0 against the rebuilt AppImage; ISSUES/STATE/journal reconciled; consistency check CRITICAL=0 HIGH=0 MED=0.

## 13. Verification commands

```bash
./run_all_tests.sh
./build_appimage.sh
OPENBOX_APPIMAGE="$PWD/OpenBox-x86_64.AppImage" OPENBOX_REQUIRE_ARTIFACTS=1 python3 -B test_packaging.py
bash scripts/lib/plan-consistency-check.sh specs/epics/e03-bug-sweep-2
```

## 17. Acceptance criteria

- [ ] Full suite exits 0.
- [ ] AppImage rebuilt and packaging tests pass against it.
- [ ] ISSUES.md closes fixed/mitigated items with `closed 2026-08-04 S30` stamps; STATE.md and journal reflect session 30.
- [ ] Candidate ledger has terminal dispositions.
- [ ] Consistency check reports CRITICAL=0 HIGH=0 MED=0.
- [ ] Residual risk and working-tree preservation are documented.

## 18. Verification script (step-by-step)

1. Run the full suite; record the outcome.
2. Rebuild the AppImage; run packaging tests with forced artifact validation.
3. Reconcile ISSUES.md, STATE.md, and the journal.
4. Run the consistency check and record the gate result.
5. Write the final summary with residual risk and working-tree preservation.
