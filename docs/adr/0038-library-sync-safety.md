# ADR 0038: Reject unsafe legacy library synchronization

Date: 2026-09-08
Status: Accepted for the corrective update

## Context

ADR 0035 described deletion propagation and two-device safety that the format-2
implementation does not deliver. Publication does not record local deletions,
refreshes every record timestamp, and can erase remote records. Pull replaces
whole records, including local launch configuration, and commits a stale games
list. A filesystem lock cannot coordinate independently replicated folders.

## Decision

Reject legacy library publish and pull before any file or library mutation.
Keep their routes registered and return an explicit service error explaining
that library synchronization is unavailable pending a safe replacement.
Keep the existing statistics synchronization policy unchanged. Preserve existing
`openbox-library.json` files for recovery and explicit future migration.

The feature engine must use a separate versioned transport namespace. Its design
must define persistent device identity, catalog-field ownership, causal edits,
durable deletion events, conflict review and recovery before it can replace this
guard. Launch paths, executable commands, credentials and unknown custom fields
must never travel through whole-record copying. Tests must establish convergence
under independently replicated folders before the corresponding UI is enabled.

## Consequences

The corrective update intentionally cannot synchronize full libraries. This is a
visible, reversible limitation preventing silent data loss. ADR 0035's claims of
deletion propagation and general two-device safety are superseded. Existing
library data and old remote files remain intact; the statistics feature remains
separate.
