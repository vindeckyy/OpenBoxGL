# Next update execution checkpoint

Started September 8, 2026 from `b4e2810` (version 1.9.0). This record covers
the supplied next-update plan through a local release-candidate audit.
Publishing, external submissions, maintainer messages, and hardware claims are
outside this execution.

## Preserved baseline and decisions

Pre-existing edits in `docs/CONTRIBUTING.md`, `docs/development/PERF.md`,
`scripts/check_tests.py`, `tests/test_check_changed_coverage.py`,
`tests/test_ci_gates.py`, `docs/NEXT_UPDATE_PLAN.md`, and ADR 0037 were kept.
The changed-line coverage floor remains 95%.

The implementation keeps JSON canonical and SQLite opt-in, preserves the v1
route contract, makes catalog sync explicit and reviewable, keeps launch
configuration and machine paths local, distinguishes LaunchBox source identity
from numeric metadata IDs, and treats shelf entries as intentional records.

## Implemented plan items

- Legacy full-library sync now fails closed with a structured 503; statistics
  sync remains available.
- Opt-in causal catalog sync has content-addressed events, device identity,
  tombstones, outbox acknowledgement, bounded validation, stale previews,
  independently selectable conflict alternatives, recovery snapshots, and
  transactional recording at the canonical state boundary. Existing libraries
  are bootstrapped when sync is enabled. Provider IDs do not implicitly merge
  distinct records.
- LaunchBox import has bounded XML parsing, explicit source IDs, path and
  emulator mappings, exclusions, deterministic preview tokens, canonical
  transactional apply, and tamper/stale-plan rejection. The browser flow
  invalidates a review when its inputs change.
- Search and facets use one canonical JSON implementation with bounded limits,
  hidden handling, library order, and an optional SQLite read model.
- Shelf projection, creation, editing, conversion, filtering, export,
  health/doctor semantics, and locale strings are wired through the UI and
  HTTP surface. Existing edits keep their entry type; conversion is explicit.
- Launch reservations are atomic by stable game ID and remain held until the
  configured tracker finishes, including wrapper-exit/child-process cases.
- Warm state transactions reuse the validated in-memory state after commit,
  reducing large-library write latency without weakening backup or recovery
  guarantees.
- Packaging includes all locales and has AppImage, Flatpak, SBOM, desktop,
  metainfo, and installed-tree checks. The AppImage relocates Python using the
  interpreter's `platlibdir` and scopes bundled loader paths.

## Evidence collected

- Baseline `make check` passed: 100 test files, 81% total coverage, 55%
  `web_app.py`; `/tmp/openbox-baseline-check.log`.
- The post-repair integrated gate passed all 107 test files with 82% total
  coverage, 55% `web_app.py`, 93% new sync-module coverage, token/i18n/frontend
  checks, and `GATE PASSED`; the final tree also passes the 29 focused
  causal-sync tests and the importer aliasing regression.
- Native WebKitGTK host build and all three native-host tests passed on the
  host and in a writable Fedora 44 aarch64/qemu build environment. The
  aarch64 native binary identified as ARM64 and all three tests passed.
- Focused Python 3.10 and 3.12 container runs passed for causal sync.
- `npm ci --ignore-scripts`, ESLint, TypeScript, and the full Puppeteer UI smoke
  suite passed. A custom browser smoke covered settings sync, LaunchBox
  migration, shelf creation/filtering, and locale loading.
- Flatpak SDK/runtime 49 build, export, bundle, user install, and installed-tree
  HTTP smoke passed. AppImage build and strict installed-tree HTTP smoke passed
  after the portability fix.
- Controlled seven-run performance sampling is recorded at
  `/tmp/openbox-perf-final.json`. Strict local budgets pass: 10k write p95
  143.4 ms, 20k write p95 291.9 ms, 10k picker p95 112.8 ms, and 20k picker
  p95 76.1 ms. Direct state-store transactions recursively isolate mutable
  caller objects; the internal HTTP path uses the validated warm transaction
  mode after its request boundary.

## Review and remaining limits

The first independent Astra review found sync initialization/identity/DAG/apply
trust issues, migration apply trust and legacy-path divergence, launch tracker
cleanup, shelf editor transitions, and AppImage relocation. Those findings have
been repaired or covered with regressions. The final focused Astra re-review is
clean; duplicate field and whole-record alternatives are independently
selectable with server-side choice validation.

Native compilation and the emulated aarch64 matrix are not a physical
handheld execution proof. The Flatpak and AppImage checks establish local
build/install behavior only. Performance budgets now pass the measured
reference-system gates. Physical handheld testing and external publication
remain outside this workstation execution.

## Acceptance ledger

- [x] Preserve baseline and pre-existing edits; complete baseline gate.
- [x] F1/F2/F7 legacy sync safety guard and HTTP regressions.
- [x] F3/F9 canonical search/facet behavior and JSON/SQLite parity.
- [x] F4/F5 unified LaunchBox planner, source identity, mappings, exclusions,
  stale review, tamper rejection, and transactional no-plan compatibility path.
- [x] Causal sync event identity, local edits/deletions, validation, transport,
  stale apply, conflict alternatives, bootstrap, and recovery backup.
- [x] Shelf projection/create/edit/convert/filter/export and launch identity
  regressions.
- [x] Atomic launch deduplication, failure retry, and reorder/delete coverage.
- [x] Five-locale coverage, frontend contract, ESLint/TypeScript, and browser
  smoke checks.
- [x] Locale packaging, Flatpak/AppImage build/install checks, SBOM and update
  metadata validation.
- [x] Native WebKitGTK compilation and socket tests.
- [x] Controlled seven-run performance budgets for 10k/20k writes and picker.
- [x] Emulated aarch64 Python 3.12 regression matrix and writable native-host
  build/tests; physical handheld testing remains unavailable.
- [x] Final post-repair `make check`, explicit changed-line report, and
  independent Astra review of the repaired sync/migration/launch paths.
- [ ] Physical handheld execution and external release publication.

External release publication and maintainer review remain explicit external
steps and are neither performed nor claimed here.
