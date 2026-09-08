# ADR 0040: Reviewable LaunchBox import decisions

Date: 2026-09-08
Status: Accepted

## Decision

Store a LaunchBox library XML `ID` as `launchbox_source_id`. Reserve
`launchbox_db_id` for a valid metadata database identifier. Never reinterpret
historical ambiguous identifiers automatically. Same-title records on different
platforms are distinct unless a verified source/provider identity links them.

Use one pure import plan for preview and apply, including duplicate rows,
exclusions, mapping choices and field preservation. Preserve user metadata by
default and require explicit replacement choices. Bind review to the source
bytes, options and local base; stale review must be repeated.

Support a bounded single XML file, explicit path-root mapping and explicit
selection of existing emulator profiles. XML never supplies executable commands.
Normalize supported media/date fields to the established OpenBox schema and
report unsupported/unresolved fields. Stage recovery before atomic commit and
reuse Activity for progress. Cancellation before commit leaves no partial import.

## Consequences

Reimport is deterministic and idempotent, including changed source paths and
duplicate rows. Title similarity is review evidence only. Multi-directory import,
artwork downloads and media copying are deferred. Existing v1 routes and global
import policy remain unchanged.
