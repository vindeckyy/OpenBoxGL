# OpenBox 1.10.0 — Sync, Import, Launch

The OpenBox release focused on safer library changes, reviewable imports, and dependable play sessions. It gives shared libraries a causal sync path, makes LaunchBox migration auditable before anything is applied, and keeps large collections responsive from search through launch.

---

## What's New

### Causal Library Sync — Shared Changes You Can Review
Opt-in catalog sync now records content-addressed events with device identity, tombstones, bounded validation, outbox acknowledgement, stale previews, and recovery snapshots. Changes are recorded transactionally at the canonical state boundary, so a shared folder can carry an auditable history instead of an opaque last-writer-wins overwrite.

Conflicts expose stable review IDs for every concurrent field and tombstone alternative. Select the title from one device, the rating from another, and the deletion decision separately; unresolved alternatives are never acknowledged silently.

The old full-library routes now fail closed before mutation with a structured unavailable response. Statistics sync remains available while the safer catalog transport is enabled explicitly.

### LaunchBox Migration — Preview Before Apply
Import a LaunchBox XML export through the migration flow. Bounded parsing creates a deterministic plan with explicit source identity, path and emulator mappings, exclusions, and a review token. Nothing changes until the plan is applied.

Plans are rejected when their inputs are stale or their payload has been tampered with. Accepted plans apply transactionally, and provider identity remains separate from numeric metadata IDs so unrelated records cannot merge by accident.

### Search, Facets & Shelf — One Library Model
Search and facets now share one canonical implementation across the HTTP surface, with bounded limits, hidden-item handling, and stable library ordering. Set `OPENBOX_ENABLE_SQLITE_READ=1` to use indexed search and GROUP BY facets for very large libraries while retaining parity checks against the JSON source of truth.

Manual and shelf entries can be created, edited, filtered, exported, and converted explicitly. Physical media, board games, and console-only titles remain intentional records without fake local paths.

### Launch Reservations — No Duplicate Starts
Launch reservations are atomic by stable game ID and remain held until the configured tracker finishes, including wrapper-exit and child-process cases. A repeated click cannot start the same game twice while its previous launch is still active.

---

## Also New

### Warm State Writes
Large-library commits reuse the validated in-memory state after a successful write, preserving backup and recovery guarantees while reducing write latency. Direct state-store transactions detach mutable caller records and results recursively, including nested containers.

### Picker Reliability
Weighted picker requests recompute their suggestion on every request, so **Again** can produce a new result. Legacy naive and timezone-aware timestamps are normalized safely when history is scored.

### Complete Linux Packaging
AppImage and Flatpak builds include all locale files, desktop metadata, SBOM and update metadata, with relocatable Python and scoped loader paths. The release audit covers x86_64 and emulated aarch64 paths plus native WebKitGTK compilation.

---

## Under the Hood

- **Fail-closed sync boundary** — unsafe legacy full-library operations are rejected before mutation, with the causal transport and conflict choices tested at the canonical state boundary
- **Source identity separation** — LaunchBox imports preserve provider IDs independently from numeric metadata IDs and reject stale or tampered migration plans
- **Warm transaction path** — validated state is reused for internal HTTP writes while direct callers receive recursive mutable-object isolation
- **Reviewable shelf records** — entry type, conversion, export, and health semantics are covered by the same state and route model
- **Release gates** — 107 test files, frontend and i18n checks, UI smoke, coverage floors, AppImage/Flatpak validation, and strict seven-run performance budgets pass on the release candidate

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |

All AppImages are signed and include SHA-256 checksums, zsync metadata for delta updates, and SBOMs. Verify with `openbox-release.pub` and `install.sh`.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.9.0...v1.10.0
