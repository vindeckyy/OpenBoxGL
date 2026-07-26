# e01s01 — Baseline and automated detector sweep

## 1. Identity

- **Story:** e01s01
- **Type:** test
- **Risk:** P1
- **BCPs:** 3
- **Context:** quality / repository-wide
- **Wave:** 0 — first 45 minutes

## 2. User value

Maintainers get a trustworthy starting point and an exact list of machine-detectable failures before any production code changes, preventing pre-existing drift or environment noise from being misreported as a new regression.

## 3. Context

The current `./run_all_tests.sh` baseline passes all 26 `test_*.py` modules with no failure signals. The working tree already contains unrelated legal, identity, UI, and packaging changes. This story records both facts, executes grouped probes around critical subsystems, and establishes the candidate ledger used by later stories.

## 4. Problem statement

A broad sweep without a frozen baseline cannot distinguish an existing defect, a dirty-tree effect, a stale artifact, or a regression introduced during today's fixes.

## 5. Purpose / callers / contracts

- **Purpose:** observe repository health and classify failures without changing product behavior.
- **Callers:** later sweep stories, maintainers reviewing findings, and final regression comparison.
- **Contracts:** exact commands and exit codes are retained; pre-existing diffs are preserved; test failures are copied verbatim; candidate status is never promoted to confirmed without reproduction.

## 6. Preconditions

- Run from repository root on Linux with Python 3.
- Do not start either UI for this story.
- Preserve the current dirty-tree file list and current commit SHA.
- Use no external credentials or network dependency.

## 7. Dependencies

- `[OK]` Python standard library.
- `[OK]` existing `run_all_tests.sh` and `test_*.py` scripts.
- No new package or abstraction.

## 8. Requirements

### ADDED: A reproducible bug-sweep baseline exists

Record timestamp, commit SHA, branch, `git status --short`, pre-existing changed paths, Python version, exact test command, exit code, warnings, and per-module completion in `specs/verifications/e01s01-baseline.md`.

### ADDED: Automated probes are grouped by product risk

Execute complete, API/security, import/integration, persistence, and packaging groups so one fail-fast run cannot hide later independent failures.

### ADDED: Machine findings enter a terminally classifiable ledger

Every signal is recorded as candidate, confirmed, rejected, duplicate, or externally blocked, with the command and evidence that produced it.

## 9. Invariants

- No production file is edited.
- No real OpenBox user-data path is read for mutation or written.
- A passing suite does not imply that manual or adversarial stories may be skipped.
- Pre-existing dirty files are listed but not reformatted or normalized.

## 10. Scope

- Repository status and environment capture.
- Full test suite.
- Python compile pass.
- Targeted subsystem test groups.
- Existing issue and recent-fix review for regression targets.
- Candidate ledger initialization.

## 11. Out of scope

- Fixing any discovered failure.
- Adding broad coverage merely to increase assertion counts.
- Live external integrations.
- AppImage rebuild unless packaging evidence shows source/artifact mismatch.

## 12. Detailed implementation steps

1. Capture commit, branch, dirty-tree paths, Python version, and the explicit list of pre-existing user-owned changes in the baseline evidence → verify: `test -f specs/verifications/e01s01-baseline.md && grep -q '^## Working-tree safety snapshot' specs/verifications/e01s01-baseline.md`
2. Run `./run_all_tests.sh`, retain the exit code and per-module output summary, and record any failure text verbatim → verify: `./run_all_tests.sh`
3. Run a repository Python compile check while excluding generated/package directories from interpretation, and record any syntax/import candidates → verify: `python3 -m compileall -q .`
4. Re-run API/security, import/integration, persistence, and packaging test groups independently so fail-fast behavior cannot mask later groups → verify: `python3 -B test_parity_api.py && python3 -B test_sessions.py && python3 -B test_updates.py && python3 -B test_secrets.py && python3 -B test_auto_import.py && python3 -B test_importers.py && python3 -B test_parity_playnite.py && python3 -B test_saves.py && python3 -B test_cloud_sync.py && python3 -B test_plugins.py && python3 -B test_packaging.py`
5. Compare current results with `.agent/ISSUES.md`, `.agent/PROJECT.md` landmines, and the latest eight commits; enter only observable signals into the candidate ledger → verify: `test -f specs/verifications/e01-candidate-ledger.md && grep -q '^## Candidates' specs/verifications/e01-candidate-ledger.md`
6. Mark every baseline signal with a current disposition and hand unresolved candidates to e01s02/e01s03 without modifying production code → verify: `! grep -Eq 'status: (new|untriaged)' specs/verifications/e01-candidate-ledger.md`

## 13. Verification commands

```bash
./run_all_tests.sh
python3 -m compileall -q .
python3 -B test_parity_api.py
python3 -B test_sessions.py
python3 -B test_updates.py
python3 -B test_secrets.py
python3 -B test_auto_import.py
python3 -B test_importers.py
python3 -B test_parity_playnite.py
python3 -B test_saves.py
python3 -B test_cloud_sync.py
python3 -B test_plugins.py
python3 -B test_packaging.py
```

## 14. Evidence format

`specs/verifications/e01s01-baseline.md` contains environment, safety snapshot, commands, exit codes, module summary, warning summary, and links to candidate IDs. `specs/verifications/e01-candidate-ledger.md` gives each candidate an ID, area, severity estimate, evidence, reproduction status, owner story, and disposition.

## 15. Failure handling

- A full-suite failure is a candidate, not an automatic product bug.
- Re-run the narrow failing module once unchanged; if it flips, measure ten runs and record the flake rate.
- Environment or stale-artifact failures are rejected only with concrete evidence.
- A test that mutates a real data root stops the sweep immediately for safety review.

## 16. Risks and mitigations

- **Dirty-tree confusion:** freeze exact paths and diffs before later edits.
- **Fail-fast masking:** run subsystem groups separately.
- **False confidence from green tests:** API and browser sweeps remain mandatory.
- **Generated bytecode noise:** do not add generated files to version control.

## 17. Acceptance criteria

- [ ] Baseline evidence identifies commit, branch, environment, dirty paths, command, and exit status.
- [ ] All 26 current test modules execute through the full runner.
- [ ] Compile and targeted groups have recorded results.
- [ ] Every observed signal has an owner story and non-new disposition.
- [ ] No production behavior or user data changed.

## 18. Verification script (step-by-step)

1. Open `specs/verifications/e01s01-baseline.md` and compare its SHA and dirty paths with Git.
2. Run the full test command exactly as recorded.
3. Run each grouped command and compare failures/warnings with the evidence.
4. Open the candidate ledger and follow every baseline candidate to its evidence and next story.
5. Confirm `git diff --stat` contains only pre-existing work plus planning/evidence files.

## 19. Stop conditions

Stop and escalate before later stories if the baseline cannot run, tests mutate real user data, repository state changes unexpectedly, or the environment cannot isolate OpenBox data safely.

## 20. Handoff

Gate: baseline and candidate ledger complete. Next: e01s02 API/failure-boundary sweep, while e01s03 consumes the same safety snapshot for browser execution.
