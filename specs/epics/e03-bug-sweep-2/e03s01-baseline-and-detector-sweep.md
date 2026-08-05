# e03s01 — Baseline and automated detector sweep

## 1. Identity

- **Story:** e03s01
- **Type:** test
- **Risk:** P1
- **BCPs:** 3
- **Wave:** 0

## 2. User value

A frozen baseline proves later findings are new defects, not pre-existing drift or dirty-tree noise.

## 3. Context

Frozen SHA `b546517` (master), Python 3.12.3, clean tree except untracked `.commandcode/`. 35 `test_*.py` modules. The e01 sweep and S28 fixed backlog exist; the open backlog I12-I18 is the seed list for this sweep.

## 4. Problem statement

Without a frozen baseline plus static scan seeds, the sweep cannot attribute defects or guard user-owned files.

## 5. Purpose / callers / contracts

Produces `specs/verifications/e03s01-baseline.md`, consumed by e03s02-e03s05 as the comparison point. Must preserve user-owned paths (`index.html` hunks aside from targeted fixes, `README.md`, `DISCLAIMER.md`, `SECURITY.md`, `TRADEMARKS.md`, `test_packaging.py`).

## 6. Assumptions

- `./run_all_tests.sh` exit 0 is the passing contract.
- `python3 -m compileall -q .` exit 0 is the syntax contract.

## 7. Exit criteria

Baseline artifact exists with working-tree snapshot, full suite, compile check, and static scan seeds.

## 13. Verification commands

```bash
./run_all_tests.sh
python3 -m compileall -q .
python3 -B test_packaging.py
```

## 17. Acceptance criteria

- [ ] Baseline artifact records the SHA, Python version, and dirty hunks.
- [ ] Full suite, compile, and packaging checks pass at the frozen SHA.
- [ ] Static scan seeds for open issues are recorded.

## 18. Verification script (step-by-step)

1. Freeze state and write the baseline artifact.
2. Run the full suite and compile check; record exact outcomes.
3. Run packaging tests; record outcome.
4. Grep for broad excepts, index access, open handles, subprocess, and threading sites; record candidate seeds.
