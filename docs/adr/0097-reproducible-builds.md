# ADR 0097: Reproducible builds and provenance

Status: Accepted as policy. The double-build report has not been run for 2.0.0.

## Context

Every release artifact has a checksum, a signature, and an SBOM. Users cannot yet confirm that a published artifact matches its source by rebuilding it.

## Decision

- Builds set `SOURCE_DATE_EPOCH` to the commit timestamp, sort archive entries, use fixed mtimes, exclude `__pycache__`, and avoid build paths in the output.
- Each artifact is built twice on separate runners and the SHA-256 of the unsigned payload is compared. Signatures are compared separately; Ed25519 signatures are expected to differ and are not a reproducibility failure.
- The report lists identical and non-identical artifacts and the cause of each difference. Reproducibility is claimed only where it was measured.

## Consequences

- A report that cannot be produced in a given environment is recorded as not measured, never as reproducible.

## Verification

Planned: `scripts/check_reproducible.py` and `docs/development/REPRODUCIBILITY.md` in the release job. Not yet in this change.
