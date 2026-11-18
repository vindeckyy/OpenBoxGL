# ADR 0108: JSON stays canonical at 50,000 games

Status: The 2026-11-13 measurement missed the RSS budget. JSON stays canonical. The formal scale stays 20,000. This ADR's 50,000-game claim does not ship, and 3.0.0 is not tagged. Split-host evidence from October and November is why the attempt was allowed; it is not the number that ships.

## Context

ADR 0098 set the rule for this release: if a JSON-canonical store meets a p95 write of 1.5 seconds or less at 50,000 games, with resident memory at or below 1.5 GB, the formal scale can rise without a storage rewrite. Otherwise 3.0 selects a storage design under a new ADR.

Two earlier runs already met that rule on different hosts:

- Apple M4, 2026-10-29, 50k writes: favorite mutation p95 927.8 ms, bulk mutation p95 1087.4 ms. Budget 1500 ms.
- Ryzen 5 4600H, 2026-11-01, 50k RSS: 1194.8 MB after the benchmark. Budget 1500 MB.

The fair SQLite comparison in ADR 0088 is a full-state commit (89 ms at 50k), not a stub write. That speed does not pay for dirty-tracking across the `transact_state` mutators ADR 0088 counted at about 121 call sites.

ADR 0105 deprecated Python 3.10 and recommended 3.12. The 2.1 startup warning already told users that 3.0 requires Python 3.11 or newer. The AppImage is built inside `ubuntu:22.04` so the highest glibc symbol stays `GLIBC_2.35` (ADR 0065). Ubuntu 22.04's distro Python is 3.10, and `build_appimage.sh` copies whatever `python3` the build machine provides.

ADR 0100 said 3.0 reads state schema 6 only for export, removes the v1 plugin runner, and removes `openbox --rollback-library`.

## Decision

- `library.json` stays the canonical store. `pkg/state/sqlite_readmodel.py` stays a derived projection: `SQLITE_AUTO_THRESHOLD = 5000`, debounce 0.75 s, 5 s max wait, `source: "json"` when stale (ADR 0101). Deleting `sqlite_readmodel.db` loses nothing.
- The formal scale stays 20,000 games. 20,000 stays the blocking perf gate (`10k_write_ms_p95` 500 ms, `20k_write_ms_p95` 1000 ms). 50,000 does not enter per-push CI. `scripts/perf_bench.py` is not retuned. The shipping run missed the RSS budget, so the scale does not rise and no new store is selected in this change.
- `JsonStateStore.load` and `load_readonly` do not migrate. Schema 7 is served. Schema greater than 7 raises `STATE_FUTURE_VERSION` and leaves the file untouched. Schema 1 through 6 raises `STATE_SCHEMA_TOO_OLD` and leaves the file untouched. Schema 6 names `openbox --migrate-library`. Schema 1 through 5 names OpenBox 2.1. Schema 0 and an unreadable file stay `StateCorruptError`.
- `migrate_locked` accepts schema 6 only, in the existing order: in-memory validate, byte copy to `library.v6.backup.json`, marker, snapshot, atomic replace, report. Schema 1 through 5 are refused without a rewrite. `MIGRATIONS` for versions 1 through 5 stay in the tree for in-memory history (`normalize_state` still migrates when a caller asks it to). The CLI does not invoke them. `save`, `recover`, and `restore_snapshot` keep that in-memory path. A schema-6 primary is not adopted by opening it, including through `recover()`.
- `openbox --rollback-library` is removed. The command exits 4 and names OpenBox 2.1.0 as the build that restores `library.v6.backup.json`. Existing backup files are not deleted. The HTTP route stays registered and returns 409. The migrate report and the startup notification no longer advertise rollback.
- Plugin API v1 is removed. `PLUGIN_API_VERSIONS` is `(2,)`. A missing `api_version` and an explicit `1` both fail with `PLUGIN_API_RETIRED` and the message `Plugin api_version is required and must be 2. OpenBox 3.0 removed the v1 runner.` `api_version` 3 keeps the unsupported-future error. `plugin_runner.py` is deleted. The v2 JSON Lines protocol is unchanged.
- The Python floor is 3.11. The recommended and bundled interpreter is 3.12. Source installs below 3.11 exit before bind. The release container stays `ubuntu:22.04` on both arches. The glibc scan stays: the highest `GLIBC_` symbol is `GLIBC_2.35`. The container installs pinned CPython `3.12.16-1+jammy1` from deadsnakes (build-time only) and points `python3` at it before `build_appimage.sh`. An assertion next to the glibc scan requires the extracted interpreter to be `>= 3.11` and `< 3.13`.
- `legacy_job.exposed_via` names `/api/v2/jobs` only. Public `/api/jobs` and `/api/v1/jobs` stay 410.
- `GET /api/v2/version` keeps `api_version: 2`, `state_schema: 7`, `plugin_api_version: 2`, `backup_format: 2`, `sync_format: 3`. Only `version` becomes `3.0.0`. A 2.1 build can still open a 3.0 library.

If the single-machine 50k run misses either budget, this ADR's scale claim does not ship and 3.0 is not tagged.

## Consequences

- A 2.1 library that is still schema 6 does not open until `openbox --migrate-library` runs, or until it is opened once on 2.1. A schema 1 through 5 library must be opened on 2.1 before 3.0 will migrate it.
- Plugins that omit `api_version`, or that still say `1`, do not load. The sample plugin is v2.
- Ubuntu 22.04 source checkouts need a separate 3.11+ interpreter. The AppImage bundles 3.12 linked against glibc 2.35.
- Five DOM state machines stay declared-only. Sessions, the media queue, and storefront scans are not moved onto `allows()`.
- Known limits that remain: the macOS signature is ad hoc, some features are Linux-only and hidden elsewhere, OpenBox 1.19 cannot read schema 7, and Flathub is not submitted.

## Verification

Reliability row 139 records this run. Rows 120 and 121 record the removed rollback command. The 20k rows stay the blocking CI gate.

Shipping measurement, 2026-11-13, working tree based on `e63e3b2`. Machine: Apple M4, macOS 27.2 arm64, Python 3.15.2, 16 GiB. Command: `python3 -B scripts/perf_bench.py --sizes 50000 --runs 3 --no-gate --memory --out build/perf-3.0-50k.json`. No soak was run.

| Metric | Result | Budget |
|---|---:|---|
| Favorite mutation p95 | 1174.2 ms | 1500 ms: met |
| Bulk mutation p95 | 1319.5 ms | 1500 ms: met |
| Server RSS after warm-up | 1190.0 MB | |
| Server RSS after the benchmark | 2163.3 MB | 1500 MB: missed |

The same run's write-path gates, which the script always measures, were inside their code budgets on this host: 10k write p95 219.3 ms (gate 500 ms) and 20k write p95 470.0 ms (gate 1000 ms).

The RSS miss is the number that counts. The formal scale stays 20,000. 3.0.0 is not tagged.
