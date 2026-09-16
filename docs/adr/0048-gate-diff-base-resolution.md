# ADR 0048: Fail loudly when the changed-line gate cannot resolve a diff base

Date: 2026-09-16
Status: Accepted

## Context

`scripts/check_tests.py` and `scripts/check_changed_coverage.py` resolved the
diff base from `@{upstream}`, falling back to `HEAD` when no upstream existed.
GitHub Actions checks out pull requests at a detached HEAD with no upstream and
a shallow fetch, so the base became `HEAD`: `git diff HEAD...HEAD` was empty,
and both the 95% changed-line floor and the new-module coverage floor silently
reported success. The gates that exist to keep coverage rising were decorative
exactly where code enters the repository.

## Decision

One resolver, `scripts/git_diff_base.py`, is shared by both gates. Resolution
order: explicit `--diff-base`, `OPENBOX_DIFF_BASE`, GitHub PR base
(`GITHUB_BASE_SHA` or the event payload), the branch upstream merge base, then
`origin/HEAD` / `origin/master` / `origin/main`. An unresolved base fails the
gate with instructions; a failing `git diff` or `git show` against the base is
also a failure instead of an empty result. `OPENBOX_DIFF_BASE=HEAD` is the
explicit opt-out for machines with no usable base ref.

CI's gate job checks out with `fetch-depth: 0` and exports the PR base SHA as
`OPENBOX_DIFF_BASE`. `tests/test_git_diff_base.py` covers the resolver and the
failure modes, and `tests/test_ci_gates.py` pins the workflow wiring.

## Consequences

Changed-line and new-module coverage now measure PRs. Contributors on a plain
`git init` repository without a remote must set `OPENBOX_DIFF_BASE` or add an
upstream; the failure message says so. Extra fetch depth increases CI checkout
time slightly, which is accepted to keep the coverage ratchet honest.
