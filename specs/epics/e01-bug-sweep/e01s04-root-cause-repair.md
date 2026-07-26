# e01s04 — Root-cause and repair confirmed P1/P2 defects

## 1. Identity

- **Story:** e01s04
- **Type:** fix
- **Risk:** P0
- **BCPs:** 8
- **Security:** high when the candidate crosses auth, secrets, paths, subprocesses, or external input
- **Wave:** 3 — maximum three-hour fix budget

## 2. User value

The most harmful defects found today are removed safely, while larger or uncertain problems are preserved as actionable evidence instead of receiving rushed speculative patches.

## 3. Context

e01s01–e01s03 produce candidates from automated, adversarial API, and browser journeys. This story handles only confirmed P1/P2 defects. Each defect is processed serially through reproduce, discriminate, root cause, impact check, red regression, smallest shared fix, targeted verification, and whole-suite verification.

## 4. Problem statement

A sweep can create more risk than it removes if symptoms are patched without proving mechanism, multiple defects are mixed in one diff, or fixes overwrite unrelated working-tree changes.

## 5. Purpose / callers / contracts

- **Purpose:** convert confirmed defects into minimal root-cause fixes with durable regression checks.
- **Callers:** affected product users, existing module callers, final regression story, and future maintainers reading bug evidence.
- **Contracts:** one defect at a time; reproduction before edit; named mechanism and trigger; caller/contract impact mapped; red test before patch; smallest shared fix; unrelated dirty hunks preserved; no unresolved security finding hidden by a generic pass.

## 6. Preconditions

- Candidate has exact reproduction evidence and provisional severity.
- Candidate is P1 or P2 and fits the remaining fix budget.
- Relevant module purpose, callers, and contracts can be stated.
- Any overlapping dirty file has a saved pre-edit diff.

## 7. Dependencies

- `[OK]` existing code and test patterns.
- `[OK]` Python standard library.
- No package addition is allowed in the same-day patch path.
- **Reason for Depth:** no new abstraction is planned; if a fix appears to require one, stop and rescope rather than inventing architecture during triage.

## 8. Requirements

### MODIFIED: Confirmed bounded P1/P2 behavior satisfies its existing contract

**Before:** A confirmed candidate reproducibly violates the specific API, persistence, launch, UI, integration, or packaging contract recorded in its `specs/bugs/BUG-*.md` evidence.

**After:** The same reproduction passes because the smallest shared root cause is corrected, neighboring callers remain green, and the bug record contains the exact defect-specific before/after behavior.

### ADDED: Every production patch has red-green proof

The regression check fails against the defective mechanism and passes after the fix; a test that only passes before and after is not accepted as evidence.

### ADDED: Findings outside the patch boundary are explicitly deferred

Redesigns, external-service dependencies, P3 findings, low-confidence symptoms, and fixes exceeding the budget receive issue entries with evidence, severity, and recommended next action.

## 9. Invariants

- Never fix on hypothesis.
- Never process two production defects in one edit cycle.
- Never weaken an assertion merely to make a suite green.
- Never overwrite unrelated legal/identity/UI/packaging hunks.
- Security, validation, data-loss prevention, and accessibility are not simplified away.

## 10. Priority algorithm

1. P1 before P2.
2. Within severity: data loss/security/auth first, then launch/settings/session/update failures, then other core workflows.
3. Prefer deterministic defects over flakes until the flake mechanism is measured.
4. Prefer high-user-impact, low-blast-radius fixes that fit the remaining budget.
5. Stop accepting new fixes when less than one hour remains; reserve that hour for e01s05.

## 11. Out of scope

- P3 fixes.
- Broad refactors or visual redesign.
- New dependencies.
- Fixes requiring live third-party credentials.
- Fixes whose root cause cannot be toggled by a discriminating check.
- More than one conceptual behavior change per defect.

## 12. Detailed implementation steps

1. Freeze the confirmed P1/P2 queue, deduplicate by mechanism, rank using the priority algorithm, and record the remaining time budget → verify: `test -f specs/verifications/e01s04-fix-ledger.md && grep -q '^## Ranked queue' specs/verifications/e01s04-fix-ledger.md`
2. For the first candidate, create/reopen `specs/bugs/BUG-*.md` with symptom, deterministic reproduction, mechanism hypotheses, discriminating checks, affected purpose/callers/contracts, and defect-specific before/after requirement → verify: `find specs/bugs -name 'BUG-*.md' -type f -print -quit | grep -q . || grep -q '^confirmed_count: 0' specs/verifications/e01s04-fix-ledger.md`
3. Run one-variable discriminating checks until the mechanism and trigger can be stated and toggled green/red; reject or defer the candidate if the gate cannot be met → verify: `! grep -Eq 'root_cause: (unknown|hypothesis)' specs/verifications/e01s04-fix-ledger.md`
4. Add the narrowest regression assertion to the closest existing test module and capture the expected failing command/output before editing production code → verify: `! grep -q 'red_evidence: missing' specs/verifications/e01s04-fix-ledger.md`
5. Apply the smallest fix at the shared root cause, preserve overlapping user-owned hunks, remove temporary instrumentation, and run the recorded targeted test → verify: `! grep -Eq 'status: (fixing|unverified)' specs/verifications/e01s04-fix-ledger.md`
6. Run affected neighboring tests, `test_secrets.py` for trust-boundary changes, and the full suite; require no new security findings in affected paths → verify: `./run_all_tests.sh && python3 -B test_secrets.py && echo 'no new security findings in affected paths'`
7. Repeat steps 2–6 serially until the queue is empty or the fix budget reaches the one-hour reserve; defer remaining items with exact rationale and next action → verify: `! grep -Eq 'status: (confirmed|queued|fixing|unverified)' specs/verifications/e01s04-fix-ledger.md`
8. Compare final diffs against the initial safety snapshot, confirm every instrumentation ledger is empty, and hand all fixed/deferred IDs to e01s05 → verify: `grep -q '^## Working-tree preservation' specs/verifications/e01s04-fix-ledger.md && ! find .agent/scratch -maxdepth 1 -name 'debug-*' -type f 2>/dev/null | grep -q .`

## 13. Per-defect execution loop

```text
reproduce → rank hypotheses → discriminate → state mechanism + trigger
→ map callers/contracts → add red regression → make smallest shared fix
→ targeted green → neighboring green → full green → remove instrumentation
→ record exact before/after + evidence → next defect
```

Each bug record carries its own runnable commands; the ledger links to them rather than inventing one generic fix command.

## 14. Evidence format

`specs/verifications/e01s04-fix-ledger.md` records queue order, candidate/bug ID, severity, reproduction command, root cause, red evidence, changed files, targeted verify, neighboring verify, full-suite result, security result, time spent, status, and defer rationale. Each `specs/bugs/BUG-*.md` contains detailed mechanism and exact requirement delta.

## 15. Failure handling

- Regression test will not fail: improve the reproduction; do not edit production code.
- Root cause cannot be toggled: defer as unconfirmed with evidence.
- Targeted fix breaks a neighbor: revert only that fix cycle and reassess the shared contract.
- Diff grows beyond narrow patch: stop and create a refactor/feature scope.
- Time reserve reached: stop accepting fixes and defer the queue.

## 16. Risks and mitigations

- **Symptom patching:** mandatory discriminating check.
- **Mixed fixes:** serial one-defect cycles and per-defect records.
- **Regression-test theater:** retain failing output from before the patch.
- **Dirty-hunk loss:** before/after file diff comparison.
- **Security regression:** rerun secrets and affected auth/path checks for trust-boundary changes.
- **Schedule collapse:** hard one-hour reserve for final verification.

## 17. Acceptance criteria

- [ ] Every confirmed P1/P2 candidate has a named mechanism and trigger.
- [ ] Every accepted fix has red-green regression evidence.
- [ ] Every fixed bug passes targeted, neighboring, and full-suite checks.
- [ ] Every non-fixed candidate has a terminal defer/reject disposition and rationale.
- [ ] No instrumentation remains.
- [ ] Unrelated working-tree changes remain intact.
- [ ] No new security findings in affected paths remain undispositioned.

## 18. Verification script (step-by-step)

1. Open the ranked queue and verify severity/order against evidence.
2. For each fixed bug, run its reproduction against the defective mechanism if safely reproducible, then run its regression against the patch.
3. Inspect the production diff and caller list to ensure the fix sits at the shared root cause.
4. Run neighboring tests and the complete test suite.
5. Compare overlapping files to the initial safety snapshot.
6. Confirm deferred findings have issue IDs and recommended next actions.

## 19. Stop conditions

Stop an individual fix when it needs redesign, a new package, live credentials, destructive real data, more than the remaining budget, or lacks root-cause proof. Stop all new fixes when one hour remains in the sweep.

## 20. Handoff

Gate: queue terminally dispositioned, all accepted fixes green, instrumentation removed, and time reserve intact. Next: e01s05 whole-product regression and handoff.
