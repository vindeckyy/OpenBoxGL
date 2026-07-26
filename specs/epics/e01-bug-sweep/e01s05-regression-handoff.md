# e01s05 — Whole-product regression and sweep handoff

## 1. Identity

- **Story:** e01s05
- **Type:** test / documentation
- **Risk:** P1
- **BCPs:** 3
- **Context:** repository-wide release confidence
- **Wave:** 4 — reserved final 60 minutes

## 2. User value

Players and maintainers get a trustworthy final state: fixed behavior remains green across neighboring systems, packaging stays coherent, unresolved findings are visible, and no unrelated work or user data was damaged.

## 3. Context

Earlier stories may add regression assertions and narrow production fixes. The initial suite passed 26 test modules, but final acceptance requires comparison with that baseline, explicit packaging checks, browser evidence reconciliation, and a durable issue/state handoff.

## 4. Problem statement

Targeted fixes can pass locally while breaking packaging lists, neighboring API flows, browser contracts, or the user's pre-existing working-tree changes. A sweep is incomplete until all evidence and issue states agree.

## 5. Purpose / callers / contracts

- **Purpose:** close the reliability loop and make the outcome reproducible by another maintainer.
- **Callers:** release work, future bug investigations, `.agent/STATE.md`, `.agent/ISSUES.md`, and project contributors.
- **Contracts:** full suite and syntax checks pass; packaging metadata remains coherent; every candidate has a terminal disposition; fixed bugs link to regression evidence; deferred bugs have issue entries; pre-existing work is preserved.

## 6. Preconditions

- e01s01 baseline exists.
- e01s02/e01s03 candidates are dispositioned.
- e01s04 fix ledger has no active item.
- Temporary servers, browser sessions, and instrumentation are stopped/removed.

## 7. Dependencies

- `[OK]` existing Python, shell, packaging tests, and AppImage builder.
- No new dependency.
- AppImage rebuild is conditional on source/manifest changes or stale-artifact evidence.

## 8. Requirements

### ADDED: Final regression evidence is comparable to baseline

Record exact command, exit code, module summary, warnings, duration, changed files, and any difference from e01s01.

### ADDED: Packaging and distribution contracts are checked

Run `test_packaging.py` unconditionally. Build the AppImage only if production/module/manifest/package content changed or packaging evidence requires it; record the decision and artifact checks.

### ADDED: Candidate, bug, issue, and state records reconcile

Every candidate links to fixed/rejected/deferred/external-block status; every fixed item links to a regression command; every deferred P1/P2 item has an `.agent/ISSUES.md` entry; `.agent/STATE.md` reports current focus/next/blockers without stale sweep state.

## 9. Invariants

- No failing verification is described as acceptable.
- No issue is closed without a passing reproduction/regression or explicit evidence it was already fixed.
- No release, commit, push, or publish occurs in this story.
- Unrelated legal/trademark work remains intact.
- Generated caches and temporary fixture data are not added to version control.

## 10. Final gate matrix

| Gate | Command/evidence | Pass condition |
|---|---|---|
| Syntax | `python3 -m compileall -q .` | exit 0 |
| Full regression | `./run_all_tests.sh` | all modules complete, exit 0 |
| API/security | critical API/session/update/secrets tests | exit 0; no undispositioned security finding |
| Packaging | `python3 -B test_packaging.py` | exit 0 |
| Browser | e01s03 checklist/artifacts | all P0/P1 rows terminal; diagnostics reviewed |
| Fix ledger | e01s04 ledger | no queued/fixing/unverified item |
| Candidate reconciliation | candidate ledger | no new/untriaged/confirmed-unowned item |
| Working-tree safety | initial vs final path/hunk review | unrelated changes preserved |
| Planning consistency | capsule checker | no CRITICAL/HIGH/MED finding |

## 11. Out of scope

- Publishing a release or opening a PR.
- Fixing newly discovered P3 findings during closure.
- Reworking release notes beyond a concise sweep summary.
- Rebuilding AppImage when no relevant packaged content changed and current packaging tests prove coherence.

## 12. Detailed implementation steps

1. Stop all sweep processes, remove temporary data/browser profiles and generated debug artifacts, and prove no real user-data path was changed → verify: `test -f specs/verifications/e01s05-final.md && grep -q '^## Cleanup and data safety' specs/verifications/e01s05-final.md`
2. Run Python compilation and the complete test suite from repository root; record exit codes, module completion, warnings, and differences from e01s01 → verify: `python3 -m compileall -q . && ./run_all_tests.sh`
3. Run the critical API/security regression group and require no new security findings in affected paths → verify: `python3 -B test_parity_api.py && python3 -B test_sessions.py && python3 -B test_updates.py && python3 -B test_secrets.py && echo 'no new security findings in affected paths'`
4. Run packaging tests; if packaged source/manifests changed or artifact structure is stale, rebuild AppImage and verify the output checksum/structure before recording the conditional decision → verify: `python3 -B test_packaging.py`
5. Reconcile candidate, browser, API, and fix ledgers so every finding is fixed, rejected, deferred, duplicate, or externally blocked with evidence → verify: `! grep -REq 'status: (new|untriaged|confirmed|queued|fixing|unverified)' specs/verifications/e01*.md`
6. Update `.agent/ISSUES.md` for deferred findings, journal the sweep in at most four lines, and rewrite `.agent/STATE.md` with focus/next/blockers and durable watch-outs only → verify: `grep -q '^## Issue and state reconciliation' specs/verifications/e01s05-final.md && test $(wc -l < .agent/STATE.md) -le 40`
7. Compare initial and final Git status/diffs, explicitly verify preservation of pre-existing changed files, and list only sweep-owned changes in the final evidence → verify: `grep -q '^## Working-tree preservation' specs/verifications/e01s05-final.md`
8. Flip task/story status to passing only after its command exits 0, sync execution status, and run capsule consistency validation → verify: `bash scripts/lib/plan-consistency-check.sh specs/epics/e01-bug-sweep && ! grep -R '^status: failing' specs/epics/e01-bug-sweep/*-tasks.yaml`

## 13. Verification commands

```bash
python3 -m compileall -q .
./run_all_tests.sh
python3 -B test_parity_api.py
python3 -B test_sessions.py
python3 -B test_updates.py
python3 -B test_secrets.py
python3 -B test_packaging.py
bash scripts/lib/plan-consistency-check.sh specs/epics/e01-bug-sweep
```

Conditional packaged-artifact gate:

```bash
bash build_appimage.sh
sha256sum OpenBox-x86_64.AppImage
python3 -B test_packaging.py
```

Run it only when packaging inputs changed during the sweep or stale-artifact evidence exists.

## 14. Evidence format

`specs/verifications/e01s05-final.md` contains cleanup/data safety, baseline comparison, command results, browser/API/fix summaries, packaging decision, issue/state reconciliation, working-tree preservation, residual risk, and final verdict. Link detailed evidence rather than duplicating large logs.

## 15. Failure handling

- Full suite failure: return to the owning defect cycle; do not mark e01s05 passing.
- Packaging failure from stale local AppImage: separate source-package consistency from stale artifact, rebuild only if required, and record both results.
- Ledger mismatch: fix records before changing code.
- Working-tree collision: stop and restore the unrelated hunk from the initial safety snapshot without discarding the bug fix.

## 16. Risks and mitigations

- **Late regression:** reserve the final hour and stop accepting fixes before it.
- **Evidence drift:** reconcile IDs across all ledgers and issue records.
- **False packaging alarm:** distinguish source manifest tests from stale built artifact.
- **Status theater:** task status changes only after command exit 0.
- **Accidental release:** release/push/publish explicitly excluded.

## 17. Acceptance criteria

- [ ] Compile, full suite, critical API/security, and packaging commands pass.
- [ ] Browser P0/P1 evidence is complete and diagnostics reviewed.
- [ ] Every candidate and confirmed bug has a terminal disposition.
- [ ] Every fixed bug links to red-green regression evidence.
- [ ] Deferred P1/P2 findings exist in `.agent/ISSUES.md`.
- [ ] `.agent/STATE.md` and journal reflect the sweep accurately.
- [ ] Pre-existing working-tree work is preserved.
- [ ] Capsule consistency checker passes and all completed task ledgers are passing.

## 18. Verification script (step-by-step)

1. Confirm no server/browser fixture process remains and temporary roots are gone.
2. Run syntax, full suite, API/security, and packaging commands exactly as recorded.
3. Compare module completion and warning profile to e01s01.
4. Walk candidate IDs and ensure each terminates in evidence, a bug fix, or an issue.
5. Inspect Git diff path-by-path, especially `index.html` and `test_packaging.py`.
6. Read `.agent/STATE.md` and `.agent/ISSUES.md` as the next maintainer would.
7. Run plan consistency and confirm task/status synchronization.

## 19. Stop conditions

Do not declare the sweep complete while any gate is red, any P1/P2 candidate lacks an owner/disposition, temporary mutation state remains, or unrelated working-tree preservation cannot be proven.

## 20. Handoff

Gate: all final checks and records pass. Next recommended workflow: `audit-code`, then `commit-message` or `release-branch` only if the user explicitly requests shipping.
