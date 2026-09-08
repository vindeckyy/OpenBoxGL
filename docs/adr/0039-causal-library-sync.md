# ADR 0039: Causal catalog synchronization

Date: 2026-09-08
Status: Accepted and enabled in 1.10.0

The release gate completed automated independent-folder exchange, conflict,
deletion/tombstone, stale-preview, recovery, transaction, and HTTP
preview/apply/publish coverage before the sync UI was enabled. Manual background
sync scheduling and physical multi-device soak remain outside this design; the
workflow is explicit and opt-in.

## Ownership and identity

JSON remains canonical. An opt-in local sync section persists a randomly generated
device UUID, observed causal history and a durable unpublished event outbox.
Game IDs remain stable on each device. Verified provider identities can establish
equivalence; otherwise an explicit game ID identifies the record. Names and
executable paths never establish cross-device identity. Ambiguous identity must
be reviewed rather than guessed.

Only an explicit catalog field allowlist is shared. Paths, launch commands,
installation configuration, credentials, unknown plugin/custom fields, transient
state and statistics are local. Statistics retain their existing separate policy.
New remote entries have unresolved local launch setup. Sharing a catalog does not
copy games, ROMs, saves or media.

## Transport and concurrency

Use a separate `openbox-library-v3/` directory containing immutable,
content-addressed causal events. Each event identifies its device, record,
parents and catalog contents or deletion. Each device publishes separate immutable
objects; no mutable shared manifest is authoritative. Local filesystem locks only
coordinate local writers. Folder replication exchanges event objects, so independent
publication cannot replace another device's unpublished records.

Record actual shared changes and deletions with the local state transaction.
Unchanged publication does not create revisions. A deletion is durable causal
history, not absence from a snapshot. A stale device's unchanged copy cannot clear
a deletion. Intentional restoration creates a later event.

Merge using causal ancestry and shared-field changes. Disjoint concurrent fields
can combine. Concurrent incompatible values and edit/delete races remain explicit
review items preserving alternatives. Applying a resolution creates a descendant
of the reviewed alternatives. Never silently select an alternative by publish time.

## Validation and commit

Validate bounded external bytes, format, object shape, hashes, unique event
identities and causal graph before merge. Preserve invalid inputs unchanged and
report an actionable error. Parsing and cloud file I/O happen outside the central
state lock. Bind preview to source content and current local state; reject a
changed base or source and require renewed review.

Before destructive apply, create a private local recovery snapshot outside the
state lock, then verify its base again at commit. Snapshot failure aborts apply.
Commit applies against current state atomically; favorite, session, import and
path changes cannot be replaced by an earlier snapshot. Pending outbox records
survive publication failure. Cancellation is allowed before the atomic commit;
the commit phase is explicitly non-cancellable.

## Compatibility and release gate

Preserve format-2 files. Old clients cannot overwrite the new transport namespace.
Do not promise downgrade compatibility. The independent exchange, conflict,
deletion, recovery, stale-preview, and HTTP regressions passed for 1.10.0, so the
explicit opt-in UI is enabled. The legacy guard in ADR 0038 remains as the
fail-closed compatibility boundary for old routes.
