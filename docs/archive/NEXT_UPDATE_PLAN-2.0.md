# OpenBox 2.0.0 implementation plan

**Status:** Proposed — for maintainer review. This document is a plan, not an execution record. No code has been changed by writing it.

**Planning baseline**

| Item | Value |
|---|---|
| Repository | `OpenBoxGL` |
| Branch | `master` |
| Baseline commit | `b3c4cecb2f090bfc98817dbc3f15db94de68f667` |
| Baseline date | 2026-10-28 13:22:50 -0400 |
| Application version | `1.19.1` (`updates.py`) |
| Working tree at planning time | Clean |
| Planning date | 2026-10-29 |
| Target release | `2.0.0` |
| Proposed stable date | 2027-03-19 (20-week program, see §18) |
| Release name | **Stable Ground** |

**How to read this document**

Every task has an ID (`W1-03`), a size (`S`, `M`, `L`, `XL`), explicit files, tests, and a "done when" condition. The document is deliberately written so that a maintainer can delete or defer a workstream without invalidating the rest. Release blockers are listed in §1.2; conditional and deferred work is listed in §1.3.

The plan uses the repository's existing conventions: JSON stays canonical, the runtime stays dependency-free, the v1 gate is replaced rather than weakened, every new runtime module is added to `runtime_modules.txt`, every new surface is added to a ratchet contract, and every visible change gets a changelog entry and an ADR.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Baseline audit](#2-baseline-audit)
3. [Scope decision](#3-scope-decision)
4. [Compatibility and deprecation policy](#4-compatibility-and-deprecation-policy)
5. [Breaking changes register](#5-breaking-changes-register)
6. [Workstreams and tasks](#6-workstreams-and-tasks)
7. [Library state format v7](#7-library-state-format-v7)
8. [API v2-only and the host channel](#8-api-v2-only-and-the-host-channel)
9. [Portable library](#9-portable-library)
10. [Plugin API v2](#10-plugin-api-v2)
11. [Backup format v2](#11-backup-format-v2)
12. [Platform release maturity](#12-platform-release-maturity)
13. [Scale and performance](#13-scale-and-performance)
14. [Upgrade experience](#14-upgrade-experience)
15. [Security and privacy](#15-security-and-privacy)
16. [Documentation and ADR plan](#16-documentation-and-adr-plan)
17. [Test and gate plan](#17-test-and-gate-plan)
18. [Schedule, milestones, and staffing](#18-schedule-milestones-and-staffing)
19. [Risk register](#19-risk-register)
20. [Release checklist](#20-release-checklist)
21. [Definition of done](#21-definition-of-done)
22. [Appendices](#22-appendices)
23. [Planning findings summary](#planning-findings-summary)

---

## 1. Executive summary

OpenBox 1.19.1 is a mature application: 359 HTTP routes, 150 settings keys, 154 runtime modules, 51 frontend modules, 223 test files, three desktop platforms, signed artifacts, a plugin API, a sync protocol, and an 86-ADR design record. The 1.x line accumulated compatibility scaffolding to get there: `/api/v1` aliases, an unversioned public API, a flat `parity_*` import bridge, optional SQLite acceleration, a one-file JSON state format, a v1 plugin protocol, and a v1 backup archive.

**OpenBox 2.0.0 is the release that turns those scaffolding decisions into stable contracts.** It is not a feature-sprawl release. It makes three promises:

1. **Your library moves with you.** A new state format (v7) plus an opt-in portable path mode lets a ROM library move between drives, machines, and operating systems without rewriting every game. A migration wizard, a compatibility report, an integrity check, and a one-command rollback make the upgrade reversible.
2. **Your integrations stop moving.** Public HTTP traffic becomes `/api/v2` only. The native host gets its own documented channel. The old v1 aliases are off by default and removed in 2.1. Backups get a versioned, hash-manifested format. Plugins get an additive v2 protocol while v1 keeps working.
3. **Every supported platform gets a clean install.** macOS becomes notarized, Windows gains an ARM64 artifact if the toolchain and runner gates pass, and the Flatpak becomes submission-ready. The existing glibc 2.35 floor, signed updater, and Ed25519 release verification stay intact.

**What 2.0 deliberately does not do**

- It does **not** make SQLite the canonical store. JSON stays canonical; SQLite remains a verified read model. A storage-engine rewrite is a 3.0 decision gated by measurements, not a 2.0 requirement.
- It does **not** raise the formal library scale from 20,000 games. 50,000 is an exploratory target with its own gate; the formal promise stays at 20,000 until a storage design meets it honestly.
- It does **not** add a hosted account, cloud service, telemetry, mobile client, plugin marketplace, or encryption-at-rest.
- It does **not** drop Python 3.10. The AppImage is built inside `ubuntu:22.04` to keep the glibc 2.35 floor (ADR 0065), and that container ships Python 3.10. Dropping 3.10 is a 2.1 decision tied to a new AppImage base.
- It does **not** rename the theme token contract. Custom themes keep working; 2.0 adds validation and a compatibility report instead of a token reset.

**The one-sentence release note**

> OpenBox 2.0 keeps your library, settings, plugins, and backups working while making the data format, the public API, the native host, and the release artifacts stable enough to last through the 2.x line.

### 1.1 What changes for each audience

| Audience | 2.0 change | Compatibility |
|---|---|---|
| Player | Portable library, migration wizard, integrity check, notarized macOS app, Windows ARM64 build (conditional) | Library auto-migrates from v6; rollback available |
| Player with a custom theme | Theme validation and a compatibility report; missing tokens are reported, not silently ignored | Tokens unchanged; themes keep working |
| Plugin author | Plugin API v2: JSON Lines runner, versioned host services, declared subscriptions | v1 manifests and hooks keep working through 2.x |
| Script author using the local API | `/api/v2` only by default; OpenAPI document; problem-details errors | v1 bridge behind `OPENBOX_ENABLE_V1_API=1` through 2.0.x, removed in 2.1 |
| Packager | Flatpak manifest and AppStream metadata submission-ready; reproducible-build report; SBOM/provenance unchanged | glibc 2.35 floor, GNOME 49 runtime |
| Maintainer | Fewer compatibility shims, clearer gates, a documented deprecation ledger, a 20-week schedule | All existing ratchets remain; new ones are added |

### 1.2 Release blockers at a glance

| ID | Blocker | Workstream |
|---|---|---|
| B-01 | State v7 migration, fault injection, and rollback pass on 1/100/10k-game fixtures | W1 |
| B-02 | Public API is v2-only; frontend and all three native hosts are migrated; v1 bridge is opt-in | W2 |
| B-03 | Portable path mode round-trips on Linux, Windows, and macOS, including Unicode and case folding | W3 |
| B-04 | Backup v2 writes and reads v1 and v2; selective restore hashes every selected file | W4 |
| B-05 | Upgrade and downgrade rehearsal passes from 1.19.1 on all three platforms | W6 |
| B-06 | macOS bundle is notarized and stapled for the stable tag | W5 |
| B-07 | `make check`, Windows suite, macOS suite, packaging, signatures, SBOM, and version sync are green | W7 |
| B-08 | No 10k/20k performance regression above 10%; 50k result is reported honestly | W7 |
| B-09 | 72-hour release-candidate soak completes | W8 |

### 1.3 Conditional and deferred work

| Item | Status | Why |
|---|---|---|
| Windows ARM64 artifact | CONDITIONAL | Depends on the WebView2 ARM64 static library and a Windows ARM64 runner or a reproducible cross-build. If either is unavailable, ship x86_64 only and add the artifact in 2.0.1. |
| Flathub submission | CONDITIONAL | External review and maintainer authorization. 2.0 ships submission-ready, not necessarily accepted. |
| 50,000-game formal gate | DEFERRED | Needs a storage design that meets the write budget. 2.0 measures it; 3.0 decides. |
| Plugin API v2 | TARGET, not blocker | Additive; v1 keeps working. If v2 slips, 2.0 still ships. |
| Canonical SQLite | DEFERRED to 3.0 | ADR 0014 rejected it; 2.0 re-validates the rejection with measurements. |
| Save sync | DEFERRED to 2.1+ | Requires conflict resolution and a much wider safety case than catalog sync. |
| Python 3.10 drop | DEFERRED to 2.1 | AppImage build container is `ubuntu:22.04`. |
| v1 API bridge removal | 2.1 | One minor cycle of escape hatch. |
| Plugin API v1 adapter removal | 3.0 | One major cycle of compatibility. |
| Theme token v2 | NOT PLANNED | No user value that justifies breaking every custom theme. |

---

## 2. Baseline audit

### 2.1 Repository inventory

| Metric | Value | Source |
|---|---:|---|
| Application version | 1.19.1 | `updates.py` |
| Runtime modules | 154 | `runtime_modules.txt` |
| Handler modules | 36 | `handlers/*.py` |
| Parity modules | 71 | `pkg/parity/parity_*.py` |
| Frontend JS modules | 51 | `static/*.js` |
| Test files under `tests/` | 223 | `tests/test_*.py` |
| Root test files | 0 | `test_*.py` |
| HTTP routes in the live contract | 359 | `scripts/contracts/routes.json` |
| Frozen v1 routes | 61 | `v1_contracts.json` |
| Settings keys | 150 | `scripts/contracts/settings.json` |
| Locales | 5 | `locales/{en,es,de,fr,pt}.json` |
| Stock themes | 6 | `themes/*.css` |
| ADRs | 86 | `docs/adr/0001`–`0086` |
| State schema | 6 | `state_store.py` |
| Plugin API | 1 | `plugins.py` |
| Library sync format | 3 | `pkg/parity/parity_library_sync.py` |
| Backup manifest | unversioned | `pkg/parity/parity_backup.py` |
| Coverage floor, total | 83.0% | `scripts/check_tests.py` |
| Coverage floor, `web_app.py` | 58.0% | `scripts/check_tests.py` |
| Changed-line floor | 95.0% | `scripts/check_tests.py` |
| New-module floor | 85.0% | `scripts/check_tests.py` |
| Python floor | 3.10 | `README.md`, `docs/SUPPORT.md` |
| Linux glibc floor | 2.35 | ADR 0065 |
| macOS bundle Python | 3.12.15 | `scripts/build_macos_app.sh` |
| Windows architecture | x86_64 | `scripts/install.ps1` |
| Flatpak runtime | `org.gnome.Platform//49` | `io.openbox.GameLauncher.yml` |

### 2.2 Current release artifacts

| Artifact | Architecture | Built on | Signature |
|---|---|---|---|
| `OpenBox-x86_64.AppImage` + `.zsync` | x86_64 | `ubuntu:22.04` container on `ubuntu-24.04` | SHA-256 + Ed25519 |
| `OpenBox-aarch64.AppImage` + `.zsync` | aarch64 | `ubuntu:22.04` container on `ubuntu-24.04-arm` | SHA-256 + Ed25519 |
| Flatpak bundle | x86_64 | release-flatpak workflow | release key |
| `OpenBox-x86_64-windows.zip` | x86_64 | `windows-latest` | SHA-256 + Ed25519 |
| `OpenBox-x86_64-windows-native-host.exe` | x86_64 | `windows-latest` | SHA-256 + Ed25519 |
| `OpenBox-macos.zip` | universal arm64 + x86_64 | `macos-15` | SHA-256 + Ed25519; bundle is ad-hoc signed |
| `OpenBox-*-sbom.json` | per artifact | release workflow | attestation |
| `community-defs` pack | — | release workflow | release key |

### 2.3 Known limits inherited from 1.19.1

These are the explicit limits the 2.0 plan must either close, preserve honestly, or defer with a reason.

1. **macOS is not notarized.** The bundle is ad-hoc signed; every build asks the user to allow it in Privacy & Security, and permissions may need re-granting after an update.
2. **Windows is x86_64 only.** `scripts/install.ps1` refuses ARM64 with a clear message; there is no ARM64 native host.
3. **Flathub submission is not done.** A Flatpak bundle is release-gated, but the store submission is a separate maintainer decision.
4. **Formal library scale is 20,000 games.** The SQLite read model is optional, auto-enables at 5,000, and is rebuilt after writes; behavior above 20,000 is exploratory.
5. **The public API is split.** `static/util.js` calls `/api/v1/*`, newer features call `/api/v2/*`, and the native host calls unversioned `/api/launch`, `/api/shutdown`, and `/api/v2/resume`.
6. **The flat `parity_*` import bridge is still installed** by `pkg/parity/__init__.py` so old flat imports resolve through a `MetaPathFinder`.
7. **Plugin API v1 is frozen** and only runs sandboxed on Linux with bubblewrap; macOS and Windows have no OS sandbox.
8. **Library paths are absolute.** A library moved to another drive or machine keeps the old paths; media and ROMs break.
9. **Backups have no format version.** A v2 backup from a future release is not distinguishable from a corrupt v1 backup.
10. **Theme validation is structural, not semantic.** A custom theme missing a token falls back at render time instead of reporting the omission.
11. **i18n coverage is key/placeholder parity, not translation completeness.** Some dialog labels remain English.
12. **The release key and updater have no downgrade path.** Once 2.0 writes a newer state, 1.x must refuse it with a clear message rather than silently misread it.

### 2.4 Technical-debt inventory

| Debt | Where | Why it matters for 2.0 | Disposition |
|---|---|---|---|
| v1 alias table | `routes.py` (`V1_ALIASED_PREFIXES`) | Doubles 61 routes and freezes an API the frontend uses | Replace with v2 canonical routes; bridge behind env flag |
| Unversioned public API | `routes.py` GET/POST tables | Mixed versioning; every new route chooses a convention | Public contract moves to `/api/v2`; host channel separate |
| Flat parity bridge | `pkg/parity/__init__.py` | A `MetaPathFinder` hides import mistakes and makes refactors risky | Remove; convert all imports to `pkg.parity.*` |
| One-file JSON state | `state_store.py` | Every write rewrites the whole library; v6 has no envelope or portable paths | v7 envelope; opt-in portable paths; runtime-only sessions separated |
| Optional SQLite read model | `pkg/state/sqlite_readmodel.py` | Advertised but optional; rebuilds O(n) after every write | Default-on with background rebuild and JSON fallback |
| Backup format | `pkg/parity/parity_backup.py` | No `format` field; no per-file hashes in the manifest | v2 manifest with per-file SHA-256; read v1 |
| Plugin v1 protocol | `plugins.py`, `plugin_runner.py` | One JSON object in/out; no streaming, cancellation, or versioned services | v2 JSON Lines protocol + v1 adapter |
| Theme validation | `scripts/check_tokens.py`, `static/app.css` | Tokens are checked in the repo, but imported themes are not validated | Runtime theme check + compatibility report |
| Version sync | `scripts/check_version_sync.py` | Only checks published spots; new format versions need their own gate | Add API/state/plugin/backup version checks |

### 2.5 What the baseline already gets right

The 2.0 plan must not regress these properties:

- The runtime is standard-library only; `scripts/check_dependencies.py` enforces it.
- State writes are atomic, backed up, snapshotted, digest-checked, and locked across processes.
- Every save and backup path rejects symlinks and path traversal.
- The v1 contract, route contract, settings contract, emulator-definition contract, frontend module graph, reliability catalog, clock coupling, and exec-bit checks are all ratcheted.
- Every release artifact has a SHA-256 sidecar, an Ed25519 signature, and an SBOM.
- The AppImage keeps the glibc 2.35 floor by building in a `ubuntu:22.04` container.
- The macOS bundle carries a pinned universal Python and a pinned CA bundle.
- The Windows installer verifies the release key anchor, the checksum, and the signature before extracting.
- The plugin sandbox strips credentials, masks the data directory, and refuses network unless the `network` permission is granted.

---

## 3. Scope decision

### 3.1 Options considered

| Option | Description | Effort | Risk | User story | Verdict |
|---|---|---:|---:|---|---|
| A. Contract reset only | Retire v1, remove the flat shim, version the backup format, bump state to v7 with no portable paths | 10–12 weeks | Low | Weak: "the API changed" | Too thin for a major |
| B. Storage rewrite | Canonical SQLite, row-level writes, 50k formal scale | 24+ weeks | High | Strong: "any size" | Deferred to 3.0 unless the spike proves a safe incremental path |
| C. Stable Ground (recommended) | Contract reset + portable library + platform maturity; scale work inside JSON + SQLite read model | 20 weeks | Medium-high | Strong: "your library moves, your integrations stop" | **Recommended** |
| D. Platform only | Notarization, ARM64, Flathub, no format changes | 10 weeks | Low-medium | Medium: "clean installs" | Not enough to justify a major |

### 3.2 Recommended direction: C, "Stable Ground"

The release has four pillars:

1. **Portable library.** State v7, opt-in portable paths, a move/rebase wizard, a media/game integrity report, and a documented rollback.
2. **Contract reset.** Public API v2-only, native host channel v2, plugin API v2, backup format v2, flat parity shim removed, v1 bridge behind an environment flag.
3. **Platform maturity.** macOS notarization, Windows ARM64 conditional, Flathub submission-ready, reproducible-build report.
4. **Upgrade safety.** A 1.19.2 preflight release, a first-run migration wizard, a compatibility report, and a downgrade path.

### 3.3 Why not canonical SQLite in 2.0

The app's state API is a mutable dict behind `transact_state(mutator)` with 121 call sites in handlers and parity modules. A canonical SQLite store would require one of:

- **Dirty tracking in every mutator.** Rewrite 121 closures to declare touched game ids and settings keys. This is a multi-month refactor with a large regression surface.
- **Full-state diff on every commit.** Hash or deep-compare 20k–50k game records per write. The current 20k favorite write is already 388 ms; a full diff would not meet the 2.0 no-regression gate.
- **A new command API.** Rewrite handlers around `ChangeSet` objects. This is a 3.0-sized architectural change.

ADR 0014 rejected canonical SQLite for compatibility; 2.0 re-validates that rejection with a measured spike (W7-01) and keeps JSON canonical. The read model becomes default-on and incrementally maintained, which captures most of the read-side benefit without changing the write contract.

### 3.4 Why portable paths rather than a full data-directory move

Moving the data directory while the server is running is unsafe: the state store, media server, saves, plugin data, and native host all hold open paths. 2.0 instead makes the **library root** portable:

- The user keeps the data directory where the platform put it.
- Game and media paths under a chosen `library_root` are stored as `@library/...` tokens.
- Moving the ROM/media folder and updating `library_root` rebases every token in one transaction.
- The data directory itself can still be moved manually with `OPENBOX_DATA_DIR`; the docs explain how.

This is the 80% use case (external drives, NAS mounts, moving a ROM collection between machines) without a live data-directory migration.

### 3.5 Non-goals

- No hosted account, cloud service, telemetry, or analytics.
- No mobile or web-hosted client.
- No 32-bit builds, Windows x86, or Linux non-glibc targets.
- No new runtime dependency; `sqlite3` and `zipfile` are stdlib.
- No encryption-at-rest for the library or backups. A future release can add it only with a dependency-free design or a reviewed exception.
- No plugin marketplace, paid plugins, or plugin code signing.
- No automatic save sync or media replication.
- No theme token rename or CSS framework migration.
- No full i18n rewrite; new keys are translated, legacy English labels stay documented.

### 3.6 Decisions needed from the maintainer

| ID | Decision | Default if unanswered |
|---|---|---|
| D-01 | Is an Apple Developer ID + notarization account available? | Build ad-hoc; mark macOS as not notarized; 2.0 still ships |
| D-02 | Is Windows ARM64 in 2.0.0 or 2.0.1? | Conditional: ship if the SDK and runner gates pass, otherwise 2.0.1 |
| D-03 | Is Flathub submission authorized? | Prepare the submission; do not open the PR without explicit approval |
| D-04 | Is the 1.19.2 preflight release approved? | Recommended; if not, fold the readiness panel into 2.0's first run |
| D-05 | Is a 20-week schedule acceptable? | Yes; cut ARM64 and Flathub first if the schedule slips |
| D-06 | Keep Python 3.10 for 2.0? | Yes; revisit in 2.1 with the AppImage base |
| D-07 | Is the v1 bridge required? | Yes, default off, removed in 2.1 |
| D-08 | Is a 50k exploratory gate wanted? | Yes, measured and reported, not a blocker |

---

## 4. Compatibility and deprecation policy

### 4.1 What a major version means for OpenBox

OpenBox is a local-first desktop application, not a hosted service. SemVer still applies, with these definitions:

- **Major** (`2.0.0`): a change that requires user action, breaks a documented external contract, or makes data unreadable by the previous major line. Examples: state schema v7, API v1 removal, backup format v2, native host channel v2.
- **Minor** (`2.1.0`): additive routes, hooks, settings, formats that the previous minor can ignore; deprecations announced but not enforced.
- **Patch** (`2.0.1`): fixes only; no new surface, no format change, no migration.

A release that changes a format but auto-migrates and offers rollback is still major because a downgrade is not transparent.

### 4.2 Deprecation windows

| Surface | Announcement | Default-off | Removal |
|---|---|---|---|
| `/api/v1/*` | 2.0.0 release notes + `Deprecation` header | 2.0.0 (`OPENBOX_ENABLE_V1_API=1` re-enables) | 2.1.0 |
| Unversioned public `/api/*` | 2.0.0 docs | 2.0.0: no longer public; internal aliases remain routable through 2.0.x | 2.1.0 |
| Plugin API v1 | 2.0 plugin docs | Still on | 3.0.0 |
| Backup v1 write | 2.0 release notes | v2 is default; v1-compatible export available | 2.1.0 |
| Python 3.10 | 2.1 release notes | — | 2.1.0 or later |
| Flat ad-hoc macOS signature | 2.0 release notes | Notarized for stable | 2.0.0 if secrets exist |
| Windows x86_64-only | 2.0 release notes | ARM64 conditional | 2.0.1 if deferred |

### 4.3 Version advertisement

Every response that can be affected by a format change advertises the contract versions. `GET /api/v2/version` returns:

```json
{
  "version": "2.0.0",
  "api_version": 2,
  "state_schema": 7,
  "plugin_api_version": 2,
  "backup_format": 2,
  "sync_format": 3,
  "platform": "linux",
  "python": "3.12.15"
}
```

The native host sends `X-OpenBox-API-Version: 2` on the host channel. The host refuses a response whose advertised major is not 2, rather than parsing a future shape.

### 4.4 Data downgrade policy

- 2.0 keeps `library.v6.backup.json` next to the migrated `library.json` for the life of the install until the user deletes it.
- `openbox --rollback-library` (implemented in `web_app.py`/`openbox.py`) restores the v6 copy after a confirmation and a second backup of the current v7 file.
- 1.19.x cannot read v7. A 1.19.2 patch adds a startup check that detects a v7 `library.json` and prints: "This library was upgraded by OpenBox 2.0. Install 2.0 or restore `library.v6.backup.json`." It must never try to normalize v7 as v6.
- After 2.1, the v6 backup is retained but the rollback command is removed. After 3.0, v6 support is removed entirely.

### 4.5 Settings retirement policy

A settings key may be removed only when:

1. It has had no reader and no writer for at least two minor releases (verified by a grep gate).
2. A migration moves its value into the replacement key or drops it with a named reason.
3. `scripts/contracts/settings.json` records a `retired` entry with `key`, `removed_in`, `migration`, and `reason`.
4. The compatibility report names the retired key and the replacement.

2.0 does not retire settings wholesale. It adds `library_root`, `path_mode`, and `media_manifest_enabled`; the audit in W1-09 may retire a small list only if the above conditions hold.

---

## 5. Breaking changes register

Each entry is a contract the 2.0 release changes. The register is the source for release notes, ADRs, and the compatibility report.

| ID | Change | User impact | Migration | Rollback | Gate |
|---|---|---|---|---|---|
| B-01 | Public API moves to `/api/v2` only | Scripts calling `/api/v1` or unversioned `/api/*` stop by default | Use `/api/v2`; or set `OPENBOX_ENABLE_V1_API=1` through 2.0.x | Unset the env flag | `scripts/check_api_contract.py`, `tests/test_api_v2_contract.py` |
| B-02 | Unversioned API is no longer public; the host channel is `/api/host/*` | Unversioned paths remain routable as an internal compatibility layer through 2.0.x, but are undocumented and not in OpenAPI | New clients use `/api/v2` or `/api/host`; old hosts keep `/api/launch`, `/api/shutdown`, `/api/v2/resume` until 2.1 | Reinstall the old host; the server keeps aliases | `scripts/check_native_paths.py`, C compile jobs |
| B-03 | State schema v7 | 1.x cannot read the library after migration | Automatic v6→v7 migration; `library.v6.backup.json` kept | `openbox --rollback-library` | `tests/test_state_v7.py`, `tests/test_state_migration_rollback.py` |
| B-04 | Portable path mode | Paths under `library_root` become `@library/...` tokens | Opt-in; `path_mode` starts `absolute` | Set `path_mode=absolute`; tokens are expanded on read | `tests/test_portable_paths.py`, `tests/test_library_move_http.py` |
| B-05 | Backup format v2 | 1.x cannot read a v2 backup | v2 is default; "1.19-compatible backup" writes v1 | Restore from a v1 backup | `tests/test_backup_format_v2.py`, `tests/test_backup_diff.py` |
| B-06 | Native host channel v2 | New hosts must use `/api/host/*` | Update all three C hosts | Old endpoints remain through 2.0.x | `tests/test_native_ipc.py`, `tests/test_native_host.py` |
| B-07 | Plugin API v2 | v1 plugins keep working; v2 adds a runner protocol | Manifest `api_version: 2`; v1 adapter | Remove the v2 manifest and keep v1 | `tests/test_plugin_api_v2.py`, `tests/test_plugin_runner.py` |
| B-08 | Flat `parity_*` import bridge removed | None for users; internal imports must use `pkg.parity.*` | Codemod all flat imports | Revert the commit | `scripts/check_flat_imports.py` |
| B-09 | Error envelope changes to problem details | Scripts parsing `error` still get it alongside `detail` | Read `detail`/`code`; `error` is kept in 2.x | — | `tests/test_api_problem_details.py` |
| B-10 | `v1_contracts.json` retired | CI/docs only | Replaced by `api_contracts.json`; v1 archived | Restore the file from git | `scripts/check_api_contract.py` |
| B-11 | macOS notarization | First launch no longer needs "Open Anyway" | Automatic with a notarized bundle | Reinstall the ad-hoc bundle | `scripts/check_macos_release.py`, manual `spctl` |
| B-12 | Windows ARM64 artifact (conditional) | ARM64 users get a native artifact instead of a refusal | Installer selects the ARM64 zip | Use the x86_64 build under emulation | `tests/test_install_scripts.py`, manual ARM64 smoke |
| B-13 | Python floor unchanged at 3.10 | None in 2.0 | — | — | `scripts/check_version_sync.py` docs |

**Non-breaking changes included in 2.0**

- Portable library (opt-in).
- Theme validation and compatibility report.
- Integrity check and repair.
- SQLite read model default-on with fallback.
- Plugin API v2 (additive).
- Windows ARM64 artifact selection (when shipped).
- OpenAPI document and `GET /api/v2/version`.
- 1.19.2 preflight panel (separate release).

---

## 6. Workstreams and tasks

The work is divided into ten workstreams plus a release program. W9 (plugin API v2) is additive and runs in parallel; it is not on the critical path. Sizes are relative: `S` ≤ 1 day, `M` 2–4 days, `L` 5–10 days, `XL` > 10 days.

### W0 — Program setup and baseline

| ID | Task | Files | Tests / evidence | Done when | Size |
|---|---|---|---|---|---|
| W0-01 | Create `release/1.19` maintenance branch; freeze 1.19.1 as the upgrade baseline | `.github/workflows/ci.yml` (branch filters), `docs/RELEASE_NOTES.md` | `git branch --show-current`; CI run on the branch | Branch exists; CI green; 1.19.1 artifacts retained | S |
| W0-02 | Record the 1.19.1 baseline: `make check`, `./run_all_tests.sh`, perf 10k/20k, packaging builds | `docs/development/PERF.md` (new baseline section) | Commands in §20.1 | Numbers recorded with date and machine | M |
| W0-03 | Write the compatibility matrix and deprecation policy (this document, tightened into ADR 0087) | `docs/adr/0087-2.0-compatibility-policy.md` | ADR reviewed | ADR accepted before any breaking change merges | S |
| W0-04 | Spike: canonical SQLite feasibility and cost | `docs/adr/0088-storage-direction.md` (draft) | Prototype harness under `/tmp`; no production code | Decision recorded: JSON stays canonical, or a 3.0 proposal with measurements | L |
| W0-05 | Spike: Windows ARM64 WebView2 SDK + runner availability | `docs/archive/HANDOFF-WINDOWS-ARM64.md` (new) | `nuget` package listing; `windows-11-arm` runner probe; `cl /?` ARM64 tools | Go/no-go recorded for W5-03 | M |
| W0-06 | Spike: macOS notarization secrets and hardened-runtime behavior | `docs/archive/HANDOFF-MACOS-NOTARY.md` (new) | Local `codesign`/`notarytool` dry run if secrets exist | Go/no-go recorded for W5-01 | M |
| W0-07 | Create the 1.19.2 preflight plan (recommended, not a blocker) | `docs/archive/NEXT_UPDATE_PLAN-1.19.2.md` | Review | Maintainer approves or declines D-04 | S |
| W0-08 | Set up a `2.0` project board / task ledger mirroring this document | `.github/` project (external) | Task IDs match | All W1–W9 IDs present | S |
| W0-09 | Baseline security review checklist for 2.0 | `docs/SECURITY.md` (addendum) | Review | Threat model deltas identified | S |

### W1 — Library state format v7 and portable paths

**Objective:** a versioned, self-describing state envelope that preserves every unknown field, separates runtime-only state from durable state, and supports opt-in portable paths.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W1-01 | Add `format`, `library_id`, `created_with`, `path_mode`, `library_root` to `default_state()` and the v7 schema | `state_store.py` | `tests/test_state_v7.py` | New state has all fields; unknown fields survive a round-trip | M |
| W1-02 | Implement `_migrate_v6_to_v7` and register it in `MIGRATIONS` | `state_store.py` | `tests/test_state_v7.py` | v6 fixture migrates; idempotent second run; future schema refused | M |
| W1-03 | Stop persisting `active_sessions`; write `sessions.json` for crash recovery and strip the key from `library.json` | `state_store.py`, `openbox.py`, `pkg/state/` | `tests/test_state_v7.py`, `tests/test_sessions.py` | `active_sessions` never appears in a written `library.json`; sessions recover after restart | M |
| W1-04 | Add the migration report and the pre-migration v6 copy | `state_store.py`, `openbox.py` | `tests/test_state_migration_rollback.py` | Report contains schema before/after, counts, skipped paths, and backup path; v6 copy is byte-identical | M |
| W1-05 | Add `--check-library`, `--repair-library`, `--migrate-library`, `--rollback-library` to the launcher CLI | `openbox.py`, `web_app.py`, `openbox.sh`, `openbox.ps1`, `openbox.cmd` | `tests/test_cli_library.py` | Commands work without starting the server; exit codes documented | M |
| W1-06 | Fail closed on a future schema with a clear message and a docs link | `state_store.py`, `api_errors.py` | `tests/test_state_v7.py` | v8 fixture returns `STATE_FUTURE_VERSION`, not a traceback | S |
| W1-07 | Add the v7 schema fixture corpus (1, 100, 10k games; malformed; truncated; unknown fields) | `tests/fixtures/state/v6/` | `tests/test_state_v7.py` | Corpus runs in CI in under 60 s | M |
| W1-08 | Document the state format in `docs/state-format.md` | `docs/state-format.md` | Docs review | Format, fields, migration, rollback, and integrity rules are documented | S |
| W1-09 | Audit settings for retirement candidates; add `library_root`, `path_mode`, `media_manifest_enabled` | `settings_schema.py`, `handlers/settings.py`, `scripts/contracts/settings.json` | `tests/test_settings_schema.py` | New keys validate; no key removed without a retired ledger entry | M |
| W1-10 | Add a schema ratchet: `scripts/check_state_schema.py` | `scripts/check_state_schema.py`, `scripts/contracts/state.json` | `tests/test_ci_gates.py` | A change to `STATE_SCHEMA_VERSION` requires a contract update | M |
| W1-11 | Add the integrity check API and UI | `handlers/library.py`, `static/health.js`, `static/settings.js` | `tests/test_library_integrity.py` | Reports missing, unreadable, and out-of-root paths with counts | L |
| W1-12 | Add the pre-app migration server and page: minimal routes, `static/migrate.js`, progress polling, apply/rollback, then hand off to the full app | `web_app.py`, `handlers/setup.py`, `static/migrate.js`, `static/setup.js`, `index.html`, `locales/*.json` | `tests/test_upgrade_flow.py` | A v6 library opens the migration page before the full UI; the page shows backup path, progress, report, and rollback | L |

**W1 acceptance criteria**

- A v6 library with unknown fields migrates to v7 without losing any field.
- The v6 backup is written and fsynced before the v7 file replaces the primary.
- A crash after any step leaves either the original v6 file or a complete v7 file, never a truncated hybrid.
- `--rollback-library` restores the v6 backup and reports that post-migration changes were discarded.
- No code path writes `active_sessions` to `library.json`.
- A v7 library opened by 1.19.2 produces the upgrade message, not a normalization attempt.

### W2 — API v2-only and the native host channel

**Objective:** one public versioned API (`/api/v2`), one documented host channel (`/api/host`), a generated OpenAPI document, and no v1 aliases by default.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W2-01 | Add `visibility` to `Route` (`public`, `host`, `internal`) and validate it at registration | `routes/registry.py`, `routes.py` | `tests/test_routes_registry.py` | A public route not under `/api/v2` fails at import | M |
| W2-02 | Replace `V1_ALIASED_PREFIXES` with `_apply_v2_aliases`; generate `/api/v2` aliases for every base route | `routes.py` | `tests/test_api_v2_contract.py` | Every current public operation resolves under `/api/v2` | L |
| W2-03 | Add the opt-in v1 bridge (`OPENBOX_ENABLE_V1_API=1`) with `Deprecation` and `Sunset` headers | `routes.py`, `api_errors.py` | `tests/test_api_v2_contract.py` | Bridge off by default; bridge on reproduces the 1.19.1 v1 surface | M |
| W2-04 | Remove the flat `parity_*` MetaPathFinder; convert every flat import | `pkg/parity/__init__.py`, `openbox.py`, `web_app.py`, all `parity_*` imports | `tests/test_flat_imports.py`, `scripts/check_flat_imports.py` | `rg "import parity_"` returns only `pkg.parity` imports; gate passes | M |
| W2-05 | Switch `static/util.js` to `/api/v2`; add a frontend endpoint contract test | `static/util.js`, `static/state.js` | `tests/test_frontend_endpoint_contract.py` | Every endpoint in `ENDPOINTS` resolves in the route registry; no `/api/v1` in `static/` | M |
| W2-06 | Move the native host to `/api/host/launch`, `/api/host/resume`, `/api/host/shutdown`, `/api/host/capabilities` | `native_host.c`, `native_host_win.c`, `native_host_mac.m`, `handlers/native.py` | `tests/test_native_ipc.py`, `scripts/check_native_paths.py` | All three hosts compile and call the host channel; old endpoints remain internal aliases | L |
| W2-07 | Add problem-details errors (`type`, `title`, `status`, `detail`, `code`, `request_id`, `retryable`) while keeping `error` | `api_errors.py`, `web_app.py`, `static/util.js` | `tests/test_api_problem_details.py` | Errors are `application/problem+json`; frontend shows the same messages | M |
| W2-08 | Generate `docs/openapi.json` from the registry; add `scripts/check_openapi.py` | `scripts/gen_api_docs.py`, `docs/openapi.json`, `scripts/check_openapi.py` | `tests/test_openapi.py` | Every public route is listed; duplicate paths fail; core schemas validate | L |
| W2-09 | Add `GET /api/v2/version` and the host header check | `handlers/native.py`, `web_app.py` | `tests/test_api_v2_contract.py` | Response carries all contract versions; host refuses a major mismatch | S |
| W2-10 | Replace `v1_contracts.json` with `api_contracts.json`; update the Makefile, CI, and docs | `v1_contracts.json` (archive), `api_contracts.json`, `scripts/check_api_contract.py`, `.github/workflows/*.yml`, `Makefile`, `docs/README.md` | `tests/test_ci_gates.py` | v1 contract archived; API contract pins public + host surfaces | L |
| W2-11 | Add the deprecation ledger and a gate for sunset dates | `docs/api-deprecations.md`, `scripts/check_deprecations.py` | `tests/test_ci_gates.py` | Every deprecation has an owner, announcement, and removal version | S |
| W2-12 | Update `docs/api-v2.md`, `docs/native-host-contract.md`, and the README REST section | `docs/api-v2.md`, `docs/native-host-contract.md`, `README.md` | Docs review | Docs match the live registry | M |

**W2 acceptance criteria**

- `GET /api/v2/library`, `/api/v2/settings`, `/api/v2/launch`, and every other frontend call work.
- No frontend module or `index.html` contains `/api/v1`.
- The native hosts call the host channel; the old endpoints still work as internal aliases through 2.0.x.
- The OpenAPI document lists every public route and no v1 route.
- The v1 bridge is off by default and does not affect the route contract count when off.
- `make check` passes with the new contract scripts.

### W3 — Portable library

**Objective:** a game/media library that can move between drives and machines without breaking paths.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W3-01 | Add `pkg/state/paths.py`: encode/decode, containment, separator normalization, case folding | `pkg/state/paths.py`, `runtime_modules.txt` | `tests/test_portable_paths.py` | Pure functions; no I/O; 100% branch coverage on POSIX and Windows rules | M |
| W3-02 | Tokenize `game["path"]` and extra-app paths when under `library_root` | `catalog.py`, `handlers/library.py`, `pkg/state/paths.py` | `tests/test_portable_paths.py` | Round-trip preserves the original absolute path; paths outside the root stay absolute | L |
| W3-03 | Resolve tokens in launch, archives, media, integrity, and health paths | `openbox.py`, `pkg/state/launch.py`, `handlers/media.py`, `handlers/library.py`, `pkg/parity/parity_repair.py` | `tests/test_portable_paths.py`, `tests/test_launch_phases.py`, `tests/test_launch_env.py` | A tokenized game launches; a missing token reports the resolved path | L |
| W3-04 | Add `library_root` and `path_mode` settings validation | `settings_schema.py`, `handlers/settings.py` | `tests/test_settings_schema.py` | Root must be absolute; mode is `absolute` or `portable`; an invalid value is rejected | S |
| W3-05 | Add `POST /api/v2/library/move/preview` and `/apply` | `handlers/library.py`, `pkg/parity/parity_library_move.py` (new), `runtime_modules.txt` | `tests/test_library_move_http.py` | Preview lists tokenizable, out-of-root, missing, and conflicting paths; apply is transactional | L |
| W3-06 | Add the "Move library" wizard UI | `index.html`, `static/settings.js`, `static/setup.js`, `locales/*.json` | `tests/test_upgrade_flow.py` | User can browse for a new root, preview, apply, and see a result | L |
| W3-07 | Add the media manifest (`media/manifest.json`) with size, mtime, SHA-256, and source | `handlers/media.py`, `pkg/state/media_probe.py`, `pkg/parity/parity_media.py` | `tests/test_media_manifest.py` | Imported/downloaded media gets a manifest entry; a missing hash is repaired on demand | L |
| W3-08 | Add integrity verification for media and game files (on-demand, not every read) | `handlers/library.py`, `static/health.js` | `tests/test_library_integrity.py` | Report distinguishes missing, changed, untracked, and out-of-root | M |
| W3-09 | Cross-platform path tests: Windows drive letters, UNC, POSIX, Unicode NFC/NFD, case-insensitive volumes | `tests/test_portable_paths.py` | Windows + macOS CI | Same library opens on all three platforms; no path is silently rewritten | M |
| W3-10 | Document portable mode, limits, and the data-directory distinction | `docs/portable-library.md`, `README.md`, `docs/SUPPORT.md` | Docs review | User docs explain what moves and what does not | S |

**W3 acceptance criteria**

- With `path_mode=portable` and `library_root=/games`, `/games/roms/x.iso` is stored as `@library/roms/x.iso`.
- Changing `library_root` to `/mnt/games` resolves the same token to `/mnt/games/roms/x.iso` without rewriting the game.
- A path outside the root stays absolute and is reported as non-portable.
- A token cannot escape the root with `..`, a symlink, or a Windows drive-relative path.
- Media under the data directory is not silently moved; the manifest reports it as local.
- The move wizard preview always matches the apply result, or the apply refuses with `MOVE_PLAN_STALE`.

### W4 — Backup format v2

**Objective:** a versioned, hash-manifested backup that can be read by 2.x interchangably and restored selectively with integrity checks.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W4-01 | Add `format: 2`, `app_version`, `state_schema`, `created_by`, and per-file SHA-256/size/mode to the manifest | `pkg/parity/parity_backup.py` | `tests/test_backup_format_v2.py` | Manifest validates; a missing hash is refused | M |
| W4-02 | Read v1 and v2 manifests; write v2 by default | `pkg/parity/parity_backup.py`, `handlers/health.py` | `tests/test_backup_format_v2.py` | A v1 backup restores; a v2 backup restores; an unknown format refuses with a clear error | M |
| W4-03 | Add the "1.19-compatible backup" export option | `handlers/health.py`, `static/settings.js` | `tests/test_backup_format_v2.py` | v1-compatible archive restores on a 1.19 fixture | M |
| W4-04 | Verify every selected file's hash before restore; report per-file results | `pkg/parity/parity_backup.py`, `static/sessions.js` | `tests/test_backup_diff.py` | A corrupted member fails before any write | M |
| W4-05 | Add a backup-format ratchet and fixture corpus | `scripts/check_backup_format.py`, `scripts/contracts/backup.json` | `tests/test_ci_gates.py` | Format changes require a contract update | S |
| W4-06 | Document backup v2, retention, and cross-version restore | `docs/backups.md`, `README.md` | Docs review | Docs list what restores where and what is local-only | S |

### W5 — Platform release maturity

**Objective:** every supported platform gets a clean, trustworthy install; conditional ARM64 and Flathub work is gated.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W5-01 | macOS notarization: Developer ID signing, hardened runtime, entitlements, `notarytool`, stapling, `spctl` verification | `scripts/build_macos_app.sh`, `scripts/macos/OpenBox.entitlements`, `.github/workflows/release.yml` | `scripts/check_macos_release.py`, manual `spctl`/`stapler` | Stable tag produces a notarized, stapled bundle; `spctl --assess` passes | XL |
| W5-02 | Update macOS install docs and release notes; remove the "not notarized" caveat when the artifact is notarized | `scripts/install-macos.sh`, `docs/SUPPORT.md`, `README.md`, `docs/RELEASE_NOTES.md` | Docs review | No stale caveat when `NOTARIZED=1` | S |
| W5-03 | Windows ARM64: parameterize the native host build for `arm64`, select `arm64/WebView2LoaderStatic.lib`, publish `OpenBox-arm64-windows.zip` | `scripts/build_native_host_windows.ps1`, `.github/workflows/release.yml`, `.github/workflows/ci.yml` | `tests/test_install_scripts.py`, `tests/test_packaging.py`, manual ARM64 smoke | ARM64 zip installs and launches on Windows 11 ARM64 | XL |
| W5-04 | Installer/updater architecture selection for ARM64 | `scripts/install.ps1`, `updates.py`, `scripts/run_windows_tests.py` | `tests/test_install_scripts.py`, `tests/test_updates.py` | ARM64 host selects the ARM64 artifact; x64 host selects x64; mismatch refuses | M |
| W5-05 | Flathub submission-ready: manifest, AppStream, screenshots, runtime re-verify, checklist | `io.openbox.GameLauncher.yml`, `openbox.metainfo.xml`, `docs/flathub-checklist.md` | `tests/test_packaging.py`, `scripts/validate_flatpak_manifest.py` | Submission checklist has no code blockers; external PR only with approval | L |
| W5-06 | Reproducible-build report: two clean builds, compare hashes, document differences | `scripts/check_reproducible.py`, `docs/development/REPRODUCIBILITY.md` | Script run on a release candidate | Report lists identical/non-identical artifacts and why | M |
| W5-07 | SBOM and provenance: ensure every new artifact has both | `.github/workflows/release.yml`, `scripts/gen_sbom.py` | `tests/test_packaging.py` | ARM64/macOS artifacts carry SBOM + attestation | M |
| W5-08 | Release key and signature verification for new artifacts | `scripts/sign_release.py`, `scripts/verify_release.py`, `docs/RELEASE_NOTES.md` | `tests/test_packaging.py` | Every published artifact has `.sha256` and `.sig`; installer verifies | M |
| W5-09 | Manual hardware matrix execution and evidence | `docs/archive/2.0-PLATFORM-EVIDENCE.md` | Manual | Each row in §17.5 has a result and a machine description | L |

### W6 — Upgrade experience and compatibility report

**Objective:** a user upgrading from 1.19.1 understands what changes, can roll back, and does not lose plugins, themes, or settings.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W6-01 | Post-migration report and rollback dialog in the full app: backup path, counts, warnings, "Roll back", "Open the library" | `static/setup.js`, `index.html`, `handlers/setup.py`, `locales/*.json` | `tests/test_upgrade_flow.py` | The report is shown once, is reachable later from Settings, and every failure has a retry or rollback | L |
| W6-02 | Compatibility report: plugins, themes, settings, sync folder, backups, API clients | `pkg/parity/parity_compat.py`, `handlers/setup.py`, `static/settings.js`, `runtime_modules.txt` | `tests/test_compat_report.py` | Report is deterministic and names each item and its disposition | L |
| W6-03 | Theme validation: required tokens, unknown tokens, missing fallbacks | `scripts/check_tokens.py`, `pkg/parity/parity_compat.py`, `static/settings.js` | `tests/test_theme_compat.py` | A theme missing a token is reported; the UI still renders | M |
| W6-04 | Plugin compatibility report: v1, v2, unknown API, sandbox status | `plugins.py`, `pkg/parity/parity_compat.py`, `static/settings.js` | `tests/test_plugin_api_v2.py` | A v1 plugin is marked compatible; an unknown API is refused with a reason | M |
| W6-05 | What's New 2.0 page and release notes integration | `static/whatsnew.js`, `locales/*.json`, `docs/RELEASE_NOTES.md` | `tests/test_frontend_contract.py` | Title uses a template; no hardcoded version in locales | S |
| W6-06 | Settings > About shows state schema, API version, plugin API, backup format | `static/settings.js`, `handlers/native.py` | `tests/test_native_handlers.py` | All four values visible and copied by diagnostics | S |
| W6-07 | Error surfaces for every new failure code | `api_errors.py`, `static/util.js`, `locales/*.json` | `tests/test_api_problem_details.py` | `STATE_FUTURE_VERSION`, `STATE_MIGRATION_FAILED`, `MOVE_PLAN_STALE`, `BACKUP_FORMAT_UNSUPPORTED`, `PLUGIN_API_UNSUPPORTED` all render | M |
| W6-08 | 1.19.2 preflight release: readiness panel, v6 backup export, deprecation notice | 1.19 branch only | 1.19.2 tests | Panel identifies blockers; export restores on 1.19.1 | M |

### W7 — Quality, security, performance, and gates

**Objective:** prove the release instead of describing it.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W7-01 | 50k exploratory benchmark and storage decision memo | `scripts/perf_bench.py`, `docs/development/PERF.md`, `docs/adr/0088-storage-direction.md` | Benchmark evidence | Numbers recorded for 10k/20k/50k; decision is explicit | L |
| W7-02 | SQLite read model default-on with background rebuild and JSON fallback | `pkg/state/sqlite_readmodel.py`, `pkg/state/cache.py`, `handlers/library.py` | `tests/test_sqlite_readmodel.py`, `tests/test_readmodel_concurrency.py` | A write triggers a debounced rebuild; a stale read falls back; no request blocks on rebuild | L |
| W7-03 | Move facets into `static/worker.search.js` with a versioned protocol | `static/worker.search.js`, `static/library.js`, `tests/js/` | `tests/js/pure.test.mjs`, browser perf | Facet time is off the UI thread; measured CLS and long tasks improve | L |
| W7-04 | No-regression performance gate at 10k/20k | `scripts/perf_bench.py`, `.github/workflows/ci.yml` | p95 comparison | No gate regresses >10% versus the 1.19.1 baseline | M |
| W7-05 | New tests: state, API, portable paths, plugin v2, backup v2, upgrade | `tests/` | All new files | New modules ≥88% coverage; changed lines ≥95% | XL |
| W7-06 | Raise coverage floors after measurement (total 85, web_app 62) | `scripts/check_tests.py` | `make check` | Floors rise; never lower | S |
| W7-07 | Security review: path traversal, symlinks, zip bombs, plugin payloads, host channel auth | `docs/SECURITY.md` addendum, tests | `tests/test_path_traversal.py`, `tests/test_plugins.py`, `tests/test_backup_format_v2.py` | Each finding has a test or a documented acceptance | L |
| W7-08 | Accessibility smoke: keyboard-only migration, focus order, reduced motion | `tests/ui_smoke` (if present), manual | Manual evidence | No keyboard trap; reduced motion respected | S |
| W7-09 | Documentation truth pass: changelog, release notes, version sync, PARITY, reliability | `docs/*`, `README.md`, `openbox.metainfo.xml` | `make version-check`, `scripts/check_reliability_catalog.py` | All version surfaces agree; every `Tested` row names a real test | L |
| W7-10 | 72-hour soak on release-candidate artifacts | `docs/archive/2.0-SOAK.md` | Manual | No crash, no state corruption, no unbounded memory | L |

### W8 — Release execution

| ID | Task | Files | Tests / evidence | Done when | Size |
|---|---|---|---|---|---|
| W8-01 | Alpha.1: contracts frozen, v1 bridge off, state v7 behind a flag | Tag `v2.0.0-alpha.1` | CI + smoke | Artifacts install; upgrade path documented | M |
| W8-02 | Alpha.2: portable library + move wizard | Tag `v2.0.0-alpha.2` | CI + manual | Move/rebase works on all platforms | M |
| W8-03 | Beta.1: feature complete, API v2-only, plugin v2, backup v2 | Tag `v2.0.0-beta.1` | CI + soak | No known data-loss path | M |
| W8-04 | Beta.2: platform artifacts, notarization, ARM64 decision | Tag `v2.0.0-beta.2` | CI + manual | Stable artifact matrix known | M |
| W8-05 | RC.1: code freeze, docs freeze, final migration fixtures | Tag `v2.0.0-rc.1` | Full gate + 72h soak | Only release-blocking fixes after this | M |
| W8-06 | RC.2: release notes, changelog, AppStream, version sync | Tag `v2.0.0-rc.2` | `make check` | All published surfaces agree | S |
| W8-07 | Stable: tag `v2.0.0`, publish artifacts, verify signatures, announce | `docs/RELEASE_NOTES.md` | Release workflow | Every artifact verified from a clean machine | M |
| W8-08 | Post-release: 2.0.1 patch window, issue triage, rollback decision | GitHub issues | Weekly review | First patch within 2 weeks if needed | M |

### W9 — Plugin API v2 (parallel, additive)

W9 runs alongside W4–W7. It is not on the critical path: if it slips, 2.0 ships with v1 plugins working exactly as they do in 1.19.1, and v2 moves to 2.1.

| ID | Task | Files | Tests | Done when | Size |
|---|---|---|---|---|---|
| W9-01 | Manifest v2: `api_version: 2`, `min_openbox`, `subscriptions`, unchanged permission whitelist | `plugins.py`, `docs/plugin-api-v2.md` | `tests/test_plugin_api_v2.py` | v2 manifest validates; unknown API refused; v1 still defaults to 1 | M |
| W9-02 | Runner v2: JSON Lines request/response, timeout/cancel, 4 MiB output cap | `plugin_runner_v2.py`, `plugins.py`, `runtime_modules.txt` | `tests/test_plugin_runner.py` | Multi-request and service round-trip work; a hung plugin is killed | L |
| W9-03 | Host services v2: versioned descriptors, `library.search`, `media.stat`, read-only enforcement | `pkg/parity/parity_plugin_services.py`, `plugins.py` | `tests/test_plugin_services.py` | Every service has a version and a bound; no write service exists | L |
| W9-04 | v1 adapter: choose the runner by `api_version`; keep v1 payloads byte-identical | `plugins.py`, `plugin_runner.py` | `tests/test_plugin_runner.py` | Every v1 test passes unchanged | M |
| W9-05 | Sandbox/trust: v2 uses the same bwrap masks; macOS/Windows status remains explicit | `plugins.py`, `tests/test_plugin_sandbox.py` | Sandbox tests | A v2 plugin is sandboxed on Linux and reported unsandboxed elsewhere | M |
| W9-06 | Compatibility report integration: v1, v2, unknown API, sandbox state | `pkg/parity/parity_compat.py`, `handlers/setup.py`, `static/settings.js` | `tests/test_compat_report.py` | Report names every plugin and its disposition | S |
| W9-07 | Docs: v2 reference, migration guide, v1 deprecation notice | `docs/plugin-api-v2.md`, `docs/plugin-api.md` | Docs review | A plugin author can port a v1 plugin without reading host source | M |
| W9-08 | Optional: a sample v2 plugin under `plugins/` used by tests | `plugins/` | `tests/test_plugin_api_v2.py` | The sample installs, runs, and is removed cleanly | S |

**W9 acceptance criteria**

- A v1 plugin installed under 1.19.1 runs unchanged under 2.0.
- A v2 plugin can declare a subscription, receive a bounded event, and return a notification.
- A v2 plugin cannot request a service more than once per hook.
- A v2 plugin that writes non-JSON output is rejected with a clear error.
- The sandbox status is truthful on every platform.
- Removing v2 support is not required for 2.0 to ship.

---

## 7. Library state format v7

### 7.1 Goals

- Make the state file self-describing, so a future version can refuse it without guessing.
- Preserve every unknown field and every existing game record.
- Separate durable state from runtime-only state.
- Add a stable library identity.
- Support opt-in portable paths without forcing a rewrite on existing users.
- Keep JSON canonical and human-inspectable.

### 7.2 Non-goals

- No row-level storage, no SQLite canonical store, no binary format.
- No automatic path rewriting during migration. Portable mode is opt-in.
- No removal of `legacy_game_ids`; old IDs remain addressable.
- No change to the game record shape beyond the new envelope and optional path tokens.

### 7.3 Envelope

`library.json` becomes:

```json
{
  "format": "openbox-library",
  "schema_version": 7,
  "library_id": "01J9Z6K3Q8V4T2R7M0X5C1B8NA",
  "created_with": "2.0.0",
  "path_mode": "absolute",
  "library_root": "",
  "games": [],
  "profiles": {},
  "history": [],
  "settings": {},
  "playlists": [],
  "queue": [],
  "notifications": [],
  "ui_state": {},
  "trash": [],
  "party_decks": [],
  "emulator_settings": {},
  "achievement_overrides": {},
  "smart_collections": []
}
```

Field rules:

| Field | Type | Rule |
|---|---|---|
| `format` | string | Must be `openbox-library` in v7; a missing value on a v6 file triggers migration |
| `schema_version` | int | `7`; a higher value is refused with `STATE_FUTURE_VERSION` |
| `library_id` | string | ULID-like, generated once at migration; stable across writes; never derived from the path |
| `created_with` | string | The app version that created or migrated the file; informational |
| `path_mode` | string | `absolute` or `portable`; invalid values are repaired to `absolute` with a warning |
| `library_root` | string | Empty, or an absolute path; a relative path is rejected |
| `active_sessions` | — | Not persisted in v7; see §7.5 |

### 7.4 Migration algorithm

The migration runs inside the state store's exclusive file lock, in the pre-app migration server described in §14.2 (not during module import). The store still normalizes on load for recovery and tests, but a user-facing upgrade uses the two-phase server so the progress and backup are visible before the full app starts.

1. Read the primary file. If it is missing, create a fresh v7 state (no migration report needed).
2. If `schema_version` parses as greater than 7, raise `StateCorruptError` with code `STATE_FUTURE_VERSION` and the minimum supported version. Do not rewrite.
3. If the file is a legacy list, wrap it as `{"games": [...], "schema_version": 1}` as today.
4. Normalize through the existing v1→v6 migrations.
5. Copy the normalized v6 object to `library.v6.backup.json` using the secure atomic writer, fsync the file and the directory, and record the backup's SHA-256.
6. Apply `_migrate_v6_to_v7`:
   - Add `format`, `schema_version: 7`, `library_id`, `created_with`, `path_mode: "absolute"`, `library_root: ""`.
   - If `active_sessions` is present and non-empty, write it to `sessions.json` with the same atomic writer, then remove it from the state.
   - Leave every game path exactly as it is.
   - Preserve unknown top-level fields and unknown game fields.
7. Validate the result with `_validate_state` and a v7-specific check (envelope fields, path mode, library root).
8. Write `library.json` atomically, write a pre-migration snapshot through the existing snapshot rotation (the report records its filename), and write `migration-report.json`.
9. Emit one notification: "Library upgraded to format 7. A v6 copy is kept at `library.v6.backup.json`."

If any step fails before step 8, the original `library.json` is untouched. If step 8 fails after the temporary file is written, the original is still untouched and the temporary is removed. If the directory fsync fails, the original may or may not have been replaced; the report records the uncertainty and the next start validates the primary and falls back to the backup.

### 7.5 Runtime-only state

`active_sessions` is process state, not library data. It is written to `sessions.json` only for crash recovery:

```json
{
  "format": 1,
  "written_at": "2026-11-10T12:00:00Z",
  "sessions": []
}
```

The in-memory `default_state()` still contains `active_sessions` so existing code keeps working. `_write_unlocked` removes it from the serialized copy. `_load_unlocked` initializes it empty and, if `sessions.json` exists, offers recovery through the existing session-resume path.

### 7.6 Migration report

`migration-report.json` is written next to the library:

```json
{
  "format": 1,
  "app_version": "2.0.0",
  "started_at": "2026-11-10T11:59:50Z",
  "completed_at": "2026-11-10T12:00:02Z",
  "from_schema": 6,
  "to_schema": 7,
  "library_id": "01J9Z6K3Q8V4T2R7M0X5C1B8NA",
  "backup": "library.v6.backup.json",
  "backup_sha256": "...",
  "counts": {
    "games": 10000,
    "profiles": 12,
    "playlists": 8,
    "sessions_moved": 2,
    "paths_tokenized": 0,
    "paths_skipped": 0
  },
  "warnings": [],
  "rollback": "openbox --rollback-library"
}
```

The report is retained until the user deletes it or runs a later migration. It is included in diagnostics (redacted) so a support request can identify the exact path taken.

### 7.7 Rollback

`openbox --rollback-library`:

1. Refuses if the process is running or an active session exists.
2. Copies the current `library.json` to `library.v7.kept.json` with a timestamp.
3. Verifies `library.v6.backup.json` parses and normalizes as v6.
4. Replaces `library.json` with the v6 backup atomically.
5. Removes `migration-report.json` and writes `rollback-report.json`.
6. Prints exactly what was lost: every game edited, added, or deleted after migration is not in the v6 copy.

The command is destructive by design and requires `--yes` in non-interactive mode.

### 7.8 Integrity check

`openbox --check-library`:

- Validates the JSON, schema, envelope, game IDs, and unknown fields.
- Checks every game path and extra-app path resolves, distinguishing missing, unreadable, out-of-root, and token-escaped.
- Checks media manifest entries if present.
- Reports counts and the first 20 examples per category.
- Exits `0` if healthy, `1` if warnings, `2` if errors. Does not mutate.

`openbox --repair-library` is deliberately narrow: it rebuilds derived caches, removes expired trash entries, and re-tokenizes paths under the current root. It never deletes games or media, and it does not overwrite `library.json.bak` unless the current state validates and the user passes `--refresh-backup`. A repair that cannot validate the primary refuses rather than replacing the last-known-good copy.

### 7.9 Test matrix for state v7

| Scenario | Fixture | Expected |
|---|---|---|
| Empty install | no `library.json` | Fresh v7 state; no migration report |
| v6, 1 game | `tests/fixtures/state/v6/one.json` | Migrates; game ID unchanged; v6 backup byte-identical |
| v6, 100 games | generated | All games preserved; unknown fields preserved |
| v6, 10k games | generated | Migration under 10 s on CI; report counts correct |
| v6 with `active_sessions` | fixture | `sessions.json` written; key removed |
| v6 with unknown top-level field | fixture | Field preserved |
| v7, already migrated | fixture | No-op; `library_id` stable |
| v8 | fixture | `STATE_FUTURE_VERSION`; no write |
| Truncated JSON | fixture | `StateCorruptError`; backup untouched |
| Disk full during write | injected | Original untouched; temp removed |
| Crash after backup, before write | injected | Next start sees v6 and retries |
| Crash after write, before report | injected | Next start sees v7 and writes the report |
| Rollback | v7 fixture | Exact v6 bytes restored; `library.v7.kept.json` exists |
| Portable token escape | fixture with `@library/../../etc/passwd` | Rejected; no read outside root |

---

## 8. API v2-only and the host channel

### 8.1 Route model

`routes/registry.py` gains a `visibility` field:

```python
@dataclass
class Route:
    method: str
    path: str
    spec: str
    func: Any = None
    public: bool = False
    v1: bool = False
    visibility: str = "public"  # public | host | internal
```

Rules enforced at registration:

- `public` paths must start with `/api/v2/`.
- `host` paths must start with `/api/host/`.
- `internal` paths may be unversioned and must be listed in `HOST_COMPAT_PATHS` or `STATIC_PATHS` with a reason.
- A duplicate `(method, path)` is an error, as today.
- The registry rejects a `public` route with `v1=True`.

### 8.2 Canonical v2 surface

`routes.py` builds the tables in this order:

1. Start from `GET_TABLE`/`POST_TABLE` (the existing base paths).
2. For every base path starting with `/api/` that does not already start with `/api/v2/`, add a `/api/v2/...` alias with the same spec.
3. Keep existing `/api/v2/...` paths unchanged.
4. Add the host channel paths (`/api/host/...`) and their internal legacy aliases.
5. If `OPENBOX_ENABLE_V1_API=1`, add the legacy `/api/v1/...` aliases for 2.0.x only.

The public contract is the v2 set. The unversioned base paths remain routable as an internal compatibility layer so old native hosts keep working through 2.0.x, but they are not advertised, not documented, not in OpenAPI, and not covered by the public contract. They are removed in 2.1. A new client must use `/api/v2` for public operations or `/api/host` for the native host channel.

### 8.3 Endpoint mapping examples

| Current call | 2.0 public call | Visibility |
|---|---|---|
| `GET /api/v1/library` | `GET /api/v2/library` | public |
| `POST /api/v1/launch` | `POST /api/v2/launch` | public |
| `GET /api/v2/library/search` | `GET /api/v2/library/search` (unchanged) | public |
| `GET /api/native/capabilities` | `GET /api/host/capabilities` | host |
| `POST /api/launch` | `POST /api/host/launch` | host |
| `POST /api/shutdown` | `POST /api/host/shutdown` | host |
| `GET /api/diagnostic` | `GET /api/v2/diagnostic` | public |

The full mapping is generated by `scripts/gen_api_docs.py` and pinned in `api_contracts.json`.

### 8.4 Frontend migration

`static/util.js` contains the endpoint map. Every entry changes from `/api/v1/...` to `/api/v2/...`. The `api()` helper adds:

- `Accept: application/json, application/problem+json`
- `X-OpenBox-API-Version: 2`
- A fallback that reads `detail` and then `error`.

A new gate, `scripts/check_api_paths.py`, scans `static/` and `index.html` for `/api/v1` and for unversioned `/api/` strings (excluding comments and the host bridge). It fails with the file and line.

### 8.5 Native host channel

The host channel is intentionally small and stable:

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/api/host/capabilities` | — | platform, version, features, data dir, native mode |
| `POST` | `/api/host/launch` | `{game_id, path?, resume?}` | `{ok, pid, session_id}` |
| `POST` | `/api/host/resume` | `{game_id}` | `{ok, session_id}` |
| `POST` | `/api/host/shutdown` | `{force}` | `{ok}` |
| `POST` | `/api/host/focus` | — | `{ok}` |

The host sends `X-OpenBox-API-Version: 2`. A 4xx with `code: HOST_API_UNSUPPORTED` tells the host to show "This OpenBox window is from a newer version" instead of sending an unknown body.

Legacy aliases (`/api/launch`, `/api/v2/resume`, `/api/shutdown`, `/api/native/capabilities`) remain internal through 2.0.x and are removed in 2.1. The `docs/native-host-contract.md` v2 documents both.

### 8.6 Problem details

`api_errors.py` gains a `problem()` helper. The JSON body becomes:

```json
{
  "type": "https://openbox.local/problems/state-future-version",
  "title": "Library format is newer than this build",
  "status": 409,
  "detail": "This library uses schema 8; this build supports 7. Install a newer OpenBox.",
  "instance": "/api/v2/library",
  "code": "STATE_FUTURE_VERSION",
  "request_id": "req-...",
  "retryable": false,
  "error": "This library uses schema 8; this build supports 7. Install a newer OpenBox."
}
```

Rules:

- `type` is a stable URI under `https://openbox.local/problems/`; it never changes.
- `code` is the machine-readable code the frontend uses.
- `error` remains for one major cycle; it duplicates `detail` for compatibility.
- `Content-Type` is `application/problem+json` for errors.
- A 500 never includes a traceback; `request_id` correlates with the log.

### 8.7 OpenAPI

`scripts/gen_api_docs.py` currently generates Markdown. It gains `--openapi`:

- Emits `docs/openapi.json`, OpenAPI 3.1.
- Every public route is a path with its method and a summary from the registry spec.
- The 40 most-used endpoints get hand-authored request/response schemas in `scripts/api_schemas.py`.
- The rest get `x-openbox-schema: untyped` and a note that the schema is not pinned.
- `scripts/check_openapi.py` fails when a public route is missing, duplicated, or has an invalid method.
- A test compares the OpenAPI path set to the route registry; drift fails.

### 8.8 API acceptance tests

| Test | What it proves |
|---|---|
| `tests/test_api_v2_contract.py` | Every public route is under `/api/v2`; every frontend call resolves; v1 is off by default; bridge reproduces v1 |
| `tests/test_api_problem_details.py` | Error shape, content type, `code`, `request_id`, `retryable` |
| `tests/test_openapi.py` | OpenAPI lists every public route, no extras, valid methods |
| `tests/test_frontend_endpoint_contract.py` | `ENDPOINTS` values resolve in the registry |
| `tests/test_native_ipc.py` | Host channel methods and legacy aliases |
| `scripts/check_api_paths.py` | No `/api/v1` in frontend |
| `scripts/check_native_paths.py` | All three C hosts call the host channel or documented aliases |

---

## 9. Portable library

### 9.1 Path model

A path is one of:

| Form | Example | Meaning |
|---|---|---|
| Absolute | `/home/me/games/x.iso` | Not portable; resolved as-is |
| POSIX absolute on Windows | `D:/Roms/x.iso` | Stored with forward slashes; resolved to `D:\Roms\x.iso` |
| Tokenized | `@library/roms/x.iso` | Resolved against `settings.library_root` |
| UNC | `//server/share/x.iso` | Not tokenizable; treated as absolute |
| Relative | `roms/x.iso` | Rejected for game paths; only tokens are relative |

### 9.2 Encoding rules

`pkg/state/paths.py`:

```python
LIBRARY_TOKEN = "@library"

def encode_path(value: str, root: str) -> str:
    """Return @library/... when value is inside root, else the normalized absolute path."""

def decode_path(value: str, root: str) -> str:
    """Expand @library/... against root; reject escapes."""

def is_within(path: str, root: str) -> bool:
    """True when path is root or a descendant, after realpath and case folding."""
```

Requirements:

- `root` must be absolute and non-empty for encoding; an empty root returns the original.
- `encode_path` uses `os.path.realpath` for containment, but stores the lexical relative path so a symlinked root does not rewrite the user's path.
- `decode_path` joins the root and the token, normalizes, and verifies containment again. A token with `..`, a NUL, a Windows drive prefix, or a leading separator is rejected.
- Windows: normalize `\` to `/` for storage; resolve back to `\`. Case-insensitive comparison uses `os.path.normcase`.
- macOS: NFC-normalize both sides for comparison, but preserve the stored string.
- Symlinks: a game path that resolves outside the root is not tokenized and is reported as `SYMLINK_OUTSIDE_ROOT`.

### 9.3 Settings

| Key | Type | Default | Validation |
|---|---|---|---|
| `library_root` | string | `""` | Absolute path or empty; must exist when non-empty |
| `path_mode` | string | `absolute` | `absolute` or `portable` |
| `media_manifest_enabled` | bool | `true` | Boolean |

A user enables portable mode in Settings > Library: choose a root, preview, apply. The preview is mandatory because it changes stored data.

### 9.4 Move/rebase wizard

`POST /api/v2/library/move/preview`:

```json
{
  "old_root": "/old/games",
  "new_root": "/new/games",
  "mode": "rebase"
}
```

Response:

```json
{
  "plan_id": "mv-...",
  "old_root": "/old/games",
  "new_root": "/new/games",
  "counts": {
    "tokenized": 1200,
    "absolute_inside_old": 3,
    "missing": 12,
    "outside": 5,
    "symlink_outside": 1
  },
  "examples": {"missing": ["/old/games/roms/a.iso"]},
  "expires_at": "2026-11-10T12:10:00Z"
}
```

`POST /api/v2/library/move/apply` requires the `plan_id`, re-computes the plan inside the transaction, and refuses with `MOVE_PLAN_STALE` if the library changed. It updates `library_root` and rewrites only paths under `old_root`. It does not copy files. The UI says clearly: "Move the folder first; OpenBox will update the paths."

This is intentionally not a file mover. File moves across filesystems are slow, resumable, and destructive; a launcher is the wrong place to implement `rsync`. The wizard rebases paths after the user moves the folder with their file manager.

### 9.5 Media manifest

`media/manifest.json`:

```json
{
  "format": 1,
  "generated_at": "2026-11-10T12:00:00Z",
  "entries": {
    "media/covers/1.png": {
      "size": 123456,
      "mtime": "2026-11-09T10:00:00Z",
      "sha256": "...",
      "source": "steamgrid"
    }
  }
}
```

Rules:

- The manifest is a cache and an integrity aid, not the source of truth for what media exists.
- A missing entry is not an error; it is "untracked".
- A hash mismatch is reported, never auto-deleted.
- Verification is on demand (Settings > Library check) or after a move; serving an image does not hash it.
- The manifest is excluded from `library.json` so it never inflates the state write.

### 9.6 Integrity report

`GET /api/v2/library/integrity`:

```json
{
  "library_id": "...",
  "path_mode": "portable",
  "library_root": "/games",
  "games": {"total": 1200, "resolved": 1180, "missing": 12, "unreadable": 3, "outside": 5},
  "media": {"total": 3400, "tracked": 3200, "missing": 100, "changed": 20, "untracked": 80},
  "warnings": [{"code": "SYMLINK_OUTSIDE_ROOT", "path": "..."}]
}
```

The UI shows counts and the first 20 examples, with a "Copy report" action. No repair runs without confirmation.

### 9.7 Limits and documentation

- Portable mode applies to game paths and extra-app paths, not to saves, screenshots, plugin data, or the data directory.
- Media under the data directory remains absolute; it moves with the data directory, not the library root.
- A library root on a removable drive must be mounted before launch; a missing root reports `LIBRARY_ROOT_MISSING` and offers to reselect it.
- The docs must be explicit that moving the folder is the user's job and that OpenBox only rebases paths.

---

## 10. Plugin API v2

### 10.1 Goals

- Add a versioned runner protocol with multiple requests per process and cancellation.
- Version the host services and the hook payload.
- Keep v1 plugins working through an adapter.
- Keep the sandbox policy and trust model unchanged.
- Avoid promising a sandbox on macOS or Windows that does not exist.

### 10.2 Manifest v2

```json
{
  "id": "com.example.scraper",
  "name": "Example Scraper",
  "version": "2.0.0",
  "api_version": 2,
  "entry": "plugin.py",
  "min_openbox": "2.0.0",
  "hooks": ["metadata", "media", "library_source"],
  "subscriptions": ["library.changed", "session.ended"],
  "permissions": ["network"],
  "settings": {"type": "object", "properties": {}},
  "commands": [{"id": "refresh", "label": "Refresh cache"}]
}
```

Rules:

- `api_version` is required for v2; a missing value means v1.
- `min_openbox` is optional; a plugin requiring a newer version is refused with a clear message.
- `subscriptions` is optional and bounded to 8.
- `permissions` remains the same whitelist (`network`); v2 adds no new permission in 2.0.
- `settings` uses the existing JSON-Schema subset.
- The checksum covers the manifest and all files, as today.

### 10.3 Runner protocol v2

Request (one JSON object per line on stdin):

```json
{"id":"1","api_version":2,"hook":"metadata","payload":{...},"services":{"version":2},"deadline_ms":15000}
```

Response (one JSON object per line on stdout):

```json
{"id":"1","ok":true,"result":{...},"notifications":[],"service_requests":[]}
```

Rules:

- The host writes one request and reads one response. If the plugin requests services, the host writes one service round-trip request and reads one response; then it closes stdin. A v2 plugin must exit when stdin closes.
- A response with an unknown `id` or a missing `ok` is invalid.
- stdout is capped at 4 MiB; stderr at 4 KiB in the error message.
- A timeout sends `SIGTERM`, waits 1 s, then `SIGKILL`.
- `service_requests` are executed by the host and returned in a second request; a second round is ignored, as today.
- The protocol is line-delimited JSON, not a framing protocol; a plugin that writes binary stdout is invalid.

### 10.4 Host services v2

The existing read-only services are versioned:

| Service | Version | Fields |
|---|---|---|
| `library.read` | 2 | game_id, name, platform, progress, favorite, playtime_seconds, tags, last_played |
| `library.search` | 2 | bounded query, limit ≤ 200, returns the same projection |
| `achievements.read` | 2 | provider, earned, total, items |
| `profile.read` | 2 | local profile fields only |
| `activity.read` | 2 | last 30 days |
| `leaderboard.read` | 2 | household records |
| `catalog.read` | 2 | catalog entries |
| `metadata.read` | 2 | metadata candidates |
| `media.read` | 2 | media kinds and URLs |
| `media.stat` | 2 | size, mtime, sha256 for an approved path |
| `notifications.write` | 2 | one bounded notification |

No v2 service writes the library. A plugin that wants to change a game returns a command result; the user applies it.

### 10.5 Compatibility adapter

- A v1 manifest is loaded by the existing path; `api_version` defaults to 1.
- The existing `plugin_runner.py` remains for v1; `plugin_runner_v2.py` is added and registered in `runtime_modules.txt`.
- The host chooses the runner by `api_version`.
- v1 hook payloads are unchanged.
- v1 plugins are not auto-upgraded; the compatibility report names them.
- Removing the v1 runner is a 3.0 task.

### 10.6 Sandbox and trust

- Linux: bubblewrap as today; the v2 runner is launched with the same masks.
- macOS and Windows: no OS sandbox; status `unavailable`; the plugin runs only if the user explicitly trusts it, and the manager says so.
- Trust remains per-plugin checksum-bound (ADR 0056). A v2 manifest change changes the checksum and requires re-approval.
- No network unless the `network` permission is granted; the sandbox re-shares the network namespace only then.
- No plugin code signing in 2.0. The release key signs the app, not plugins.

### 10.7 Tests

| Test | What it proves |
|---|---|
| `tests/test_plugin_api_v2.py` | Manifest validation, `min_openbox`, subscriptions, unknown API refusal |
| `tests/test_plugin_runner.py` | v1 runner unchanged; v2 line protocol; timeout/cancel; oversized output |
| `tests/test_plugin_services.py` | Service versions, bounds, read-only enforcement |
| `tests/test_plugin_sandbox.py` | v2 uses the same bwrap masks; macOS/Windows status |
| `tests/test_plugin_capabilities.py` | v2 capability hooks and one-round service contract |
| `tests/test_compat_report.py` | v1 vs v2 vs unknown plugin classification |

---

## 11. Backup format v2

### 11.1 Goals

- Make the backup self-describing and forward-refusable.
- Hash every archived file before restore.
- Keep v1 backups readable.
- Keep selective restore and the pre-restore safety copy.

### 11.2 Manifest

```json
{
  "format": 2,
  "app_version": "2.0.0",
  "state_schema": 7,
  "created_at": "2026-11-10T12:00:00Z",
  "created_by": "openbox",
  "items": ["library", "settings", "media"],
  "library_id": "01J9Z6K3Q8V4T2R7M0X5C1B8NA",
  "files": [
    {"name": "library.json", "size": 12345, "mode": "0600", "sha256": "..."},
    {"name": "settings.json", "size": 2345, "mode": "0600", "sha256": "..."}
  ],
  "encryption": "none"
}
```

Rules:

- `format` is required; a missing value means v1.
- `files` includes every archived member except `manifest.json` itself.
- `sha256` is lowercase hex; a missing or malformed hash refuses the archive.
- `encryption` is always `none` in 2.0; a future format can add it.
- The archive is a ZIP with mode `0600`; symlinks are refused.

### 11.3 Write path

1. Build the state and settings snapshots as today.
2. Compute the manifest from the actual members, not from the source paths.
3. Write to a temporary file, fsync, rename, fsync the directory.
4. Restrict permissions to the owner.
5. Return the archive path and a summary.

### 11.4 Read path

1. Open the archive; reject symlinks and path traversal for every member.
2. Parse `manifest.json`; if `format` is missing, treat as v1; if `format` > 2, refuse with `BACKUP_FORMAT_UNSUPPORTED`.
3. For v2, verify every selected member's hash and size before writing anything.
4. Compute the restore plan; show it to the user.
5. Create a pre-restore safety backup.
6. Apply selected files atomically; report per-file results.

### 11.5 v1 compatibility

- `restore_backup` continues to accept the v1 manifest shape.
- The backup UI offers "Create a 1.19-compatible backup" for users who need to move back.
- The v1 export omits v7-only envelope fields and writes a v6-compatible library snapshot; game records and settings are unchanged.
- Portable tokens are expanded to absolute paths in the v1-compatible snapshot, so 1.19.1 can resolve every path. The export refuses if a token cannot be resolved (for example, an unmounted library root); it never writes a v6 snapshot with `@library/...` values.

### 11.6 Tests

| Scenario | Expected |
|---|---|
| Create v2 backup | Manifest lists every member with a valid hash |
| Restore v2 | Every selected file hash checked; no write on mismatch |
| Restore v1 | Accepted; no hash check; plan still shows contents |
| Unknown format 3 | `BACKUP_FORMAT_UNSUPPORTED`; no write |
| Zip bomb | Member cap enforced; no extraction |
| Symlink member | Refused |
| Path traversal member | Refused |
| Disk full during restore | Original files intact; safety backup exists |
| Selective restore | Only selected files written; others untouched |

---

## 12. Platform release maturity

### 12.1 macOS notarization

**Required inputs**

| Secret | Purpose |
|---|---|
| `MACOS_CERT_P12` | Base64 Developer ID Application certificate |
| `MACOS_CERT_PASSWORD` | Certificate password |
| `MACOS_NOTARY_APPLE_ID` | Apple ID for notarytool |
| `MACOS_NOTARY_TEAM_ID` | Team ID |
| `MACOS_NOTARY_PASSWORD` | App-specific password |

Alternatively, an App Store Connect API key (`MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER_ID`, `MACOS_NOTARY_KEY_P8`).

**Build pipeline changes**

1. Import the certificate into a temporary keychain.
2. Build the universal bundle as today.
3. Sign every Mach-O with `codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY"`.
4. Sign the bundle with `--identifier io.openbox.GameLauncher`.
5. Verify with `codesign --verify --deep --strict --verbose=2`.
6. Create the zip with `ditto -c -k --keepParent --norsrc`.
7. Submit with `xcrun notarytool submit --wait --keychain-profile "$PROFILE"`.
8. Staple with `xcrun stapler staple`.
9. Verify with `spctl --assess --type execute --verbose=4` and `stapler validate`.
10. Publish only after all checks pass.

**Entitlements**

`scripts/macos/OpenBox.entitlements`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
  <true/>
  <key>com.apple.security.cs.disable-library-validation</key>
  <true/>
</dict>
</plist>
```

The exact set is validated by running the bundle, not by copying a template. If Python's `ctypes` or the WebKit process fails under the hardened runtime, add the narrowest entitlement that fixes it and record why in ADR 0094.

**Fallback**

If the secrets are unavailable, the release job builds an ad-hoc bundle and marks the artifact as `notarized: false` in the release notes. 2.0 can still ship, but the macOS pillar is partial and the release notes say so. Stable tags should not claim notarization without `spctl` evidence.

### 12.2 Windows ARM64 (conditional)

**Build changes**

- `scripts/build_native_host_windows.ps1` gains `-Architecture x64|arm64`.
- `Find-VcVars` selects `vcvarsarm64.bat` for ARM64.
- `Find-WebView2Sdk` checks `build/native/arm64/WebView2LoaderStatic.lib`.
- `cl` uses `/D_ARM64_` where needed and links the ARM64 static lib.
- The RC step is architecture-neutral.

**Artifacts**

| Artifact | Architecture |
|---|---|
| `OpenBox-x86_64-windows.zip` | x86_64 |
| `OpenBox-arm64-windows.zip` | ARM64 |
| `OpenBox-x86_64-windows-native-host.exe` | x86_64 |
| `OpenBox-arm64-windows-native-host.exe` | ARM64 |

**Installer/updater**

`scripts/install.ps1` maps `$env:PROCESSOR_ARCHITECTURE`:

- `AMD64` → x86_64.
- `ARM64` → arm64 if the artifact exists, otherwise a clear message with the x86_64 emulation path.
- `x86` → refuse.
- An explicit `-Architecture` overrides.

`updates.py` selects the asset by architecture, not by a hardcoded name. A missing ARM64 asset falls back to x86_64 with a warning, never to an incompatible binary.

**CI**

- If `windows-11-arm` is available, run the Windows suite and build the ARM64 host there.
- If not, cross-compile on `windows-latest` and run a manual smoke on an ARM64 VM or device.
- If neither is possible, defer the artifact to 2.0.1 and keep the installer refusal.

**Smoke evidence**

- `OpenBox-arm64-windows.zip` extracts and `scripts/install.ps1` succeeds.
- `native_host.exe` launches a WebView2 window.
- A game launches and a session records.
- The updater selects the ARM64 asset.

The Windows zip does not bundle Python; an ARM64 install needs an ARM64 CPython 3.10+ on `PATH`, just as the x86_64 zip needs an x86_64 CPython. The installer must state this when no ARM64 Python is found, rather than opening a window that cannot start.

### 12.3 Flathub submission-ready

| Item | Owner | Status at 2.0 |
|---|---|---|
| `org.gnome.Platform//49` ships `webkit2gtk-4.1` | CI `flatpak-validate` | Re-verify; record the result |
| AppStream metadata current through 2.0.0 | Release | Update `openbox.metainfo.xml` |
| Screenshots point at an existing tag | Release | Re-capture if the UI changed; pin to `v2.0.0` after the tag exists |
| Manifest repository | Maintainer | Prepare; do not open the PR without approval |
| Flathub review | External | Out of the release's control |

2.0 can ship with the Flatpak bundle and a submission-ready manifest. It cannot promise Flathub acceptance. The release notes say "submission-ready", not "available on Flathub".

### 12.4 Reproducible builds

- Set `SOURCE_DATE_EPOCH` to the commit timestamp for zip, tar, and AppImage builds.
- Sort archive entries; fixed mtimes; no `__pycache__`; no build paths.
- Build each artifact twice on separate runners and compare the SHA-256 of the unsigned payload.
- Compare signatures separately; Ed25519 signatures are expected to differ and are not a reproducibility failure.
- Report differences; do not claim reproducibility where it is not measured.
- macOS `ditto` and AppImage tooling may embed timestamps; the report records the exact difference.

### 12.5 Artifact matrix for 2.0.0

| Artifact | Architecture | Required | Conditional |
|---|---|---|---|
| Linux AppImage | x86_64 | Yes | — |
| Linux AppImage | aarch64 | Yes | — |
| Linux Flatpak | x86_64 | Yes | — |
| Windows zip | x86_64 | Yes | — |
| Windows native host | x86_64 | Yes | — |
| Windows zip | ARM64 | Conditional | W5-03 |
| Windows native host | ARM64 | Conditional | W5-03 |
| macOS bundle | universal | Yes | Notarization conditional on D-01 |
| SBOMs | all | Yes | — |
| Signatures | all | Yes | — |

---

## 13. Scale and performance

### 13.1 Policy

- **Formal scale remains 20,000 games** for 2.0. Every blocking performance gate covers 10k and 20k.
- **50,000 games is exploratory.** It is measured, reported in `docs/development/PERF.md`, and not a release blocker.
- A future 3.0 may raise the formal scale only if it meets the write and memory budgets; that decision requires a storage change.

### 13.2 Targets

| Metric | 1.19.1 baseline | 2.0 target | Gate |
|---|---:|---:|---|
| Full library, 20k | measure in W0-02 | no regression >10% | Blocking |
| Gzip library, 20k | measure | no regression >10% | Blocking |
| Favorite write, 20k | measure | no regression >10% | Blocking |
| Write path, 20k | measure | no regression >10% | Blocking |
| Picker, 20k | measure | no regression >10% | Blocking |
| Constellation, 20k | measure | no regression >10% | Blocking |
| Facet, 20k | measure | ≤ 800 ms if achievable; otherwise no regression | Blocking |
| Search layout shift | 0 at 1k/10k | 0 | Blocking |
| 50k full library | n/a | report only | Exploratory |
| 50k facet | n/a | report only | Exploratory |
| 50k write | n/a | report only | Exploratory |
| 50k memory | n/a | report only | Exploratory |
| Native host cold start | 242 ms (1.15) | ≤ 400 ms at 20k | Blocking |

Exact baseline values are recorded in W0-02 before any code changes. The target is a no-regression gate, not a claim that 2.0 is faster.

### 13.3 SQLite read model default-on

- `SQLITE_READ_MODEL` becomes enabled by default for libraries above the existing threshold and remains opt-out with `OPENBOX_ENABLE_SQLITE_READ=0`.
- A write invalidates the model; a debounced background thread rebuilds it.
- Reads during a rebuild use the JSON path and set `source: "json"`.
- `query_parity_check()` remains the safety net; a mismatch falls back and logs a warning.
- The database file is `sqlite_readmodel.db` in the data directory; it is excluded from backups.
- A corrupt database is deleted and rebuilt, never treated as data loss.
- The read model is not the source of truth; deleting it loses nothing.

### 13.4 Facet worker

- Move facet computation into `static/worker.search.js`.
- Protocol: `{type: "facets", query, generation}` → `{type: "facets", generation, counts}`.
- The main thread sends a generation number; a stale response is ignored.
- While the worker computes, the UI keeps the previous counts and shows a subtle busy state.
- The worker has no DOM access and no network.
- A fallback computes on the main thread if the worker fails to start.
- The browser benchmark measures long tasks and CLS before/after.

### 13.5 Measurement plan

1. W0-02 records the 1.19.1 baseline on the reference machine.
2. Every performance-affecting PR runs `scripts/perf_bench.py --sizes 10000,20000 --runs 5` locally and records the p95.
3. The blocking CI job runs the same gate; a regression >10% fails.
4. The 50k run uses `--sizes 50000 --no-gate` and is reported, not enforced.
5. The report includes command, machine, commit, and date. No number is copied forward.

### 13.6 Storage spike decision

W0-04 and W7-01 answer one question: **can a JSON-canonical store meet a 50k write budget with the current state API?**

- If yes (p95 write ≤ 1.5 s at 50k, memory ≤ 1.5 GB), 3.0 can raise the formal scale without a storage rewrite.
- If no, 3.0 must choose canonical SQLite or a journal-first store, with a migration plan and a new ADR.
- 2.0 does not block on either outcome.

---

## 14. Upgrade experience

### 14.1 1.19.2 preflight (recommended)

A small patch release four weeks before 2.0:

- Settings > About shows "2.0 readiness".
- Checks: custom themes with unknown tokens; plugins with an unknown API; a sync folder with format 3 events; backups with no format; a library over 20k games; a non-default data directory.
- A "Create a 2.0-ready backup" button writes a v1 backup and a copy of `library.json`.
- A notice explains the API change and links to the migration guide.
- No state schema change; 1.19.1 remains compatible.

If the maintainer declines D-04, the same checks run on 2.0's first launch before migration.

### 14.2 First run after upgrade

1. The launcher detects `schema_version < 7` before building the full handler stack.
2. It starts the **pre-app migration server** on the normal loopback port with a minimal page and routes:
   - `GET /` → migration page (`static/migrate.js`)
   - `GET /api/migration/status`
   - `POST /api/migration/apply`
   - `POST /api/migration/rollback`
   - the static assets the page needs
3. The page shows: "OpenBox 2.0 will upgrade your library. A copy is kept so you can roll back." It offers "Back up first" (default) and "Upgrade".
4. On apply, the migration runs in a worker; the page polls status with a phase and a count. The server is not serving the full app yet.
5. On success, the page shows the report: games, playlists, profiles, sessions moved, paths tokenized, warnings. It offers "Open the library" or "Roll back".
6. "Open the library" writes a completion marker and exits the pre-app server. The launcher starts the full app; the native host reconnects to the new `server.port` and `server.token` and loads `/`. The window stays open across the restart.
7. On rollback, restore v6, restart the server, and explain that changes made after migration are lost.

If the two-phase server is cut for schedule, the fallback is a pre-migration backup plus a post-start report dialog. That fallback loses live progress but never loses the backup or rollback. The plan treats live progress as desirable, not as a data-safety requirement.

### 14.3 Compatibility report

The report is a table with one row per item:

| Column | Example |
|---|---|
| Item | `com.example.scraper` |
| Kind | plugin |
| Detected | api 1 |
| Support in 2.0 | compatible via adapter |
| Action | none |
| Detail | runs unsandboxed unless trusted |

Rows are generated for:

- Plugins: v1, v2, unknown API, invalid manifest, unsandboxed.
- Themes: stock, custom complete, custom missing token, custom unknown token.
- Settings: retired keys, invalid values, unknown keys (dropped on save as today).
- Sync: format 3 folder, no folder, unreachable folder.
- Backups: v1, v2, none, unreadable.
- Library: schema, game count, path mode, missing root.
- API: v1 clients detected? (No telemetry; the server can only report whether the v1 bridge is enabled.)

The report is read-only. Actions are explicit buttons, never automatic.

### 14.4 What's New

- `static/whatsnew.js` gains a 2.0 entry.
- The title uses a template, not a hardcoded version (the 1.15 locale bug is the reason).
- Copy covers: portable library, API v2, plugin v2, backup v2, notarized macOS, ARM64 (conditional), Flathub status.
- All five locales get the new keys; English fallback is allowed only if the translation is not ready, and the release notes say so.

### 14.5 Error catalog for the upgrade

| Code | When | User action |
|---|---|---|
| `STATE_FUTURE_VERSION` | Library schema > 7 | Install a newer OpenBox |
| `STATE_MIGRATION_FAILED` | Any migration error | Retry; restore the v6 copy; report with the log |
| `STATE_BACKUP_MISSING` | Migration can't write the v6 copy | Free space; retry |
| `LIBRARY_ROOT_MISSING` | Portable root is not mounted | Reselect the root |
| `MOVE_PLAN_STALE` | Library changed between preview and apply | Re-run the preview |
| `BACKUP_FORMAT_UNSUPPORTED` | Backup format > 2 | Install a newer OpenBox |
| `PLUGIN_API_UNSUPPORTED` | Plugin requires a newer API | Update OpenBox or the plugin |
| `HOST_API_UNSUPPORTED` | Native host/server major mismatch | Restart both; reinstall the host |
| `V1_API_DISABLED` | A v1 call without the bridge | Use `/api/v2` or enable the bridge |

---

## 15. Security and privacy

### 15.1 Threat model deltas

| Change | New risk | Mitigation |
|---|---|---|
| Portable path tokens | Token escape to arbitrary files | Containment check after decode; reject `..`, NUL, drive prefixes; symlink realpath check |
| Move/rebase API | A stale plan rewrites the wrong paths | Plan ID + in-transaction revalidation; `MOVE_PLAN_STALE` |
| API v2 aliases | A public route accidentally exposed as internal | Visibility validation at import; contract gate |
| OpenAPI generation | Schema drift or leakage of internal routes | Generated from the registry; public-only filter; test |
| Plugin v2 runner | Longer-lived process, more payload | Same sandbox; per-request timeout; output cap; no new permissions |
| Backup v2 | Archive member path traversal | Existing member validation; hash check before write |
| macOS notarization | Hardened runtime entitlements too broad | Minimal entitlements; document each; review |
| Windows ARM64 | A wrong-architecture binary | Installer/updater architecture selection; signature verification |
| v1 bridge | Re-exposes a deprecated surface | Off by default; env only; sunset header; removed in 2.1 |
| Migration report | Leaks paths in diagnostics | Redact path examples; include counts only |

### 15.2 Local API authentication

Unchanged:

- `server.token` is written to the data directory with owner-only permissions and deleted on exit.
- The UI prefers `X-OpenBox-Token`; a query token is accepted only during startup.
- The host channel uses the same token; there is no unauthenticated host route.
- Static assets remain public only as today.

### 15.3 Plugin security

- v2 does not add network access by default.
- The sandbox command is unchanged for Linux.
- macOS/Windows remain unsandboxed and explicitly reported.
- The plugin data bag and credentials remain bounded and stripped from logs.
- A v2 manifest change invalidates trust until re-approved.
- No plugin can write the library directly; services are read-only.

### 15.4 Backup and migration safety

- Every write is temp-file + fsync + rename + directory fsync.
- The v6 backup is written and verified before the v7 primary replaces it.
- A restore verifies hashes before writing.
- A pre-restore safety backup is always created.
- A failed migration leaves the original file untouched.
- A corrupted primary falls back to `.bak` and the snapshot rotation as today.

### 15.5 Privacy

- No telemetry, no analytics, no remote logging.
- Diagnostics remain local; the user copies them.
- The migration report includes paths only in redacted form.
- The OpenAPI document describes the local API; it does not call home.
- Notarization sends the bundle to Apple; the app itself still has no runtime account.

### 15.6 Security review checklist

- [ ] Every new path input is validated and contained.
- [ ] Every new file write is atomic and permission-restricted.
- [ ] Every new subprocess has a timeout.
- [ ] Every new HTTP route has an authentication check unless explicitly public.
- [ ] Every new error message is safe to show and does not include secrets.
- [ ] Every new archive member is validated before extraction.
- [ ] Every new plugin payload is bounded.
- [ ] Every new release secret is scoped to the release job.
- [ ] Every new artifact has a signature, checksum, and SBOM.
- [ ] Every new environment flag is documented and has a safe default.

---

## 16. Documentation and ADR plan

### 16.1 ADRs

| ADR | Title | Contents | Status target |
|---|---|---|---|
| 0087 | 2.0 compatibility and deprecation policy | SemVer definitions, windows, sunset headers, data downgrade | Accepted before W2 |
| 0088 | Storage direction after 2.0 | JSON-canonical decision, 50k spike, 3.0 options | Accepted before W7 |
| 0089 | Library state format v7 | Envelope, runtime-only state, migration, rollback | Accepted before W1-02 |
| 0090 | Portable library | Path model, tokens, containment, move/rebase | Accepted before W3-01 |
| 0091 | API v2-only and host channel | Route visibility, aliases, problem details, OpenAPI | Accepted before W2-01 |
| 0092 | Plugin API v2 | Manifest, runner, services, v1 adapter, sandbox | Accepted before W9-01 |
| 0093 | Backup format v2 | Manifest, hashes, v1 read, selective restore | Accepted before W4-01 |
| 0094 | macOS notarization | Signing, entitlements, notarytool, fallback | Accepted before W5-01 |
| 0095 | Windows ARM64 channel | Build, artifacts, installer, runner gates | Accepted before W5-03 |
| 0096 | Flathub submission readiness | Manifest, metadata, screenshots, review boundary | Accepted before W5-05 |
| 0097 | Reproducible builds and provenance | SOURCE_DATE_EPOCH, double-build, report | Accepted before W5-06 |
| 0098 | Scale policy: 20k formal, 50k exploratory | Budgets, gates, decision rule | Accepted before W7-01 |
| 0099 | Deprecation and removal ledger | Ledger format, gate, review cadence | Accepted before W2-11 |
| 0100 | 2.0 maintenance and 2.1 plan | Patch policy, v1 bridge removal, Python floor | Accepted before stable |

### 16.2 Docs updates

| Document | Change |
|---|---|
| `README.md` | 2.0 features, install matrix, portable library, API v2, Python, scale, ARM64 status |
| `docs/CHANGELOG.md` | 2.0.0 entry; Unreleased emptied; v1 retirement note |
| `docs/RELEASE_NOTES.md` | 2.0.0 notes, upgrade instructions, known limits |
| `docs/SUPPORT.md` | Platform matrix, scale, Python, macOS notarization, ARM64 |
| `docs/PARITY.md` | 2.0 rows: portable library, API v2, plugin v2, backup v2, notarization, ARM64 |
| `docs/reliability.md` | New rows 123+ for migration, portable paths, API, plugin v2, backup v2 |
| `docs/native-host-contract.md` | Host channel v2, legacy aliases, version header |
| `docs/api-v2.md` | Regenerated; v1 archived |
| `docs/openapi.json` | New generated document |
| `docs/plugin-api.md` | v1 deprecation notice; link to v2 |
| `docs/plugin-api-v2.md` | New reference |
| `docs/state-format.md` | New |
| `docs/portable-library.md` | New |
| `docs/backups.md` | New |
| `docs/upgrade-2.0.md` | New user/engineering guide |
| `docs/api-deprecations.md` | New ledger |
| `docs/flathub-checklist.md` | Current status, 2.0 screenshots, runtime check |
| `docs/development/PERF.md` | 2.0 baseline, 50k exploratory, facet worker |
| `docs/development/REPRODUCIBILITY.md` | New report |
| `docs/README.md` | ADR range, new docs, release line |
| `openbox.metainfo.xml` | 2.0.0 release entry, screenshots |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | Advertised version 2.0.0 |

### 16.3 Version-sync surfaces

`scripts/check_version_sync.py` must continue to find the version in:

- `updates.py` (declared)
- `README.md` badge, "Latest stable", installer `VERSION=`
- `openbox.metainfo.xml` latest release
- `docs/PARITY.md` lead paragraph
- `.github/ISSUE_TEMPLATE/bug_report.yml`
- `docs/SUPPORT.md`
- `docs/SECURITY.md`
- `docs/flathub-checklist.md`
- `docs/CHANGELOG.md` dated section
- `docs/README.md` shipped-release line

New in 2.0: `docs/openapi.json` `info.version`, `api_contracts.json` `app_version`, and `scripts/contracts/state.json` `app_version` must agree. Add these checks or document why they are derived.

### 16.4 Reliability rows

Add at least 20 rows covering:

- v6→v7 migration on a full disk.
- Crash during migration.
- Rollback with post-migration edits.
- Portable root missing/unmounted.
- Token escape attempt.
- Move plan stale.
- API v1 bridge off/on.
- Host channel version mismatch.
- Plugin v2 timeout/cancel/oversized output.
- Backup v2 hash mismatch/unknown format.
- macOS notarization failure fallback.
- Windows ARM64 wrong-architecture selection.
- 50k memory exhaustion.

Each row must be `Tested` with a real test or `Manual` with a written procedure. The catalog gate enforces that every `Tested` row names an existing test.

---

## 17. Test and gate plan

### 17.1 New test files

| File | Covers |
|---|---|
| `tests/test_state_v7.py` | Envelope, migration, future schema, `library_id` |
| `tests/test_state_migration_rollback.py` | v6 backup, fault injection, rollback |
| `tests/test_cli_library.py` | `--check-library`, `--repair-library`, `--migrate-library`, `--rollback-library` |
| `tests/test_portable_paths.py` | Encode/decode, containment, separators, Unicode, case |
| `tests/test_library_move_http.py` | Preview/apply, stale plan, counts |
| `tests/test_media_manifest.py` | Manifest write, verify, missing, changed |
| `tests/test_library_integrity.py` | Report categories, read-only behavior |
| `tests/test_api_v2_contract.py` | v2 coverage, v1 off, bridge on |
| `tests/test_flat_imports.py` | No flat `parity_*` imports; canonical package only |
| `tests/test_api_problem_details.py` | Shape, content type, codes |
| `tests/test_openapi.py` | Every public route listed |
| `tests/test_frontend_endpoint_contract.py` | `ENDPOINTS` resolve |
| `tests/test_plugin_api_v2.py` | Manifest, subscriptions, `min_openbox` |
| `tests/test_backup_format_v2.py` | Manifest, hashes, v1 read, unknown format |
| `tests/test_upgrade_flow.py` | Wizard, report, rollback UI |
| `tests/test_compat_report.py` | Plugins/themes/settings/sync/backups |
| `tests/test_theme_compat.py` | Missing/unknown tokens |
| `tests/test_macos_release.py` | Workflow contains notarization, entitlements exist |
| `tests/test_windows_arm64.py` | Installer/updater architecture selection (static) |
| `tests/test_api_paths.py` | No `/api/v1` in frontend (script wrapper) |
| `tests/test_reproducible_build.py` | Double-build report parsing (skipped when not run) |

### 17.2 New scripts and gates

| Script | Purpose | Wired into |
|---|---|---|
| `scripts/check_api_contract.py` | Pin public + host routes; retired v1 ledger | `check_tests.py`, CI |
| `scripts/check_api_paths.py` | No `/api/v1` or unversioned `/api/` in frontend | `check_tests.py`, CI |
| `scripts/check_native_paths.py` | All C hosts use host channel or documented aliases | `check_tests.py`, CI |
| `scripts/check_openapi.py` | OpenAPI matches registry | `check_tests.py`, CI |
| `scripts/check_state_schema.py` | State schema ratchet | `check_tests.py`, CI |
| `scripts/check_backup_format.py` | Backup format ratchet | `check_tests.py`, CI |
| `scripts/check_flat_imports.py` | No flat `parity_*` imports | `check_tests.py`, CI |
| `scripts/check_deprecations.py` | Every deprecation has a sunset | `check_tests.py`, CI |
| `scripts/check_macos_release.py` | Notarization steps and entitlements present | `check_tests.py`, CI |
| `scripts/check_reproducible.py` | Compare two build hashes | Release job |

### 17.3 Ratchets

| Contract | Change |
|---|---|
| `scripts/contracts/routes.json` | Public v2 surface + host + internal; 61 v1 entries in `retired` with reasons |
| `scripts/contracts/settings.json` | Add 3 keys; no removal without a retired entry |
| `scripts/contracts/frontend_modules.json` | Add `static/migrate.js` (migration page); update the graph and the baseline |
| `scripts/contracts/reliability.json` | New rows |
| `scripts/contracts/clock_coupling.json` | Unchanged unless new date fixtures |
| `scripts/contracts/state.json` | New: schema version, envelope fields |
| `scripts/contracts/backup.json` | New: format, manifest fields |
| `api_contracts.json` | Replaces `v1_contracts.json` |
| `docs/openapi.json` | New generated contract |

Run `make contracts` only after a deliberate surface change. Review the diff; commit it with the change.

### 17.4 Coverage floors

Proposed after measurement:

| Floor | 1.19.1 | 2.0 target |
|---|---:|---:|
| Total | 83.0 | 85.0 |
| `web_app.py` | 58.0 | 62.0 |
| Changed line | 95.0 | 95.0 |
| New module | 85.0 | 88.0 |

Never lower a floor to clear a failure. If the new code cannot meet 88%, split or test it; do not exclude it.

### 17.5 CI matrix

| Job | OS | Python | What |
|---|---|---|---|
| `test` | ubuntu-latest | 3.10, 3.12, 3.13 | Full suite + portable gates |
| `gate` | ubuntu-latest | 3.12 | `make check`, ratchets, coverage |
| `windows` | windows-latest | 3.12 | Suite, native host build, portable gates |
| `windows-arm64` | windows-11-arm (if available) | 3.12 | Suite + ARM64 host build; conditional |
| `macos` | macos-15, macos-15-intel | 3.10, 3.12 | Suite + bundle build |
| `flatpak-validate` | ubuntu-latest | 3.12 | Manifest + AppStream |
| `perf-20k` | ubuntu-latest | 3.12 | Blocking 10k/20k gate |
| `perf-50k` | nightly | 3.12 | Exploratory report, non-blocking |
| `release` | tags | 3.12 | Build, sign, attest, publish |

### 17.6 Manual test matrix

| Area | Cases |
|---|---|
| Upgrade | 1.19.1 → 2.0.0 on Linux, Windows, macOS; empty, 100, 10k games; pre-app migration page on each |
| Downgrade | 2.0.0 → 1.19.1 via v6 backup; verify the 1.19.2 message |
| Portable | Move root on each platform; missing drive; UNC; Unicode; symlink |
| API | Every frontend action; v1 bridge on/off; OpenAPI fetch |
| Plugins | v1 simple, v1 capability, v2, malformed, timeout, unsandboxed |
| Themes | 6 stock, custom complete, custom missing token |
| Backups | v1 and v2 create/restore; selective; corrupt hash; wrong format |
| Sync | Format 3 folder; two devices; conflict; no folder |
| Emulators | RetroArch, MAME, DOSBox; Flatpak; Windows/macOS paths |
| Saves | Existing manifests; restore; missing file |
| Big Box | Controller, keyboard, pointer, video, battery |
| Localization | 5 locales; new keys; fallback |
| Accessibility | Keyboard-only, focus, reduced motion |
| Performance | 10k/20k gate; 50k report |
| Packaging | AppImage extract/run; Flatpak; Windows zip; macOS app; system install |

### 17.7 Test-writing rules

- One `test_*.py` per new runtime module.
- Standalone-script style; `python3 -B tests/test_state_v7.py`.
- No network; mock subprocess and HTTP.
- Use `OPENBOX_DATA_DIR` temp dirs; never write to the real library.
- Mark clock-coupled tests with `# clock-coupled: <mitigation>` and update `scripts/contracts/clock_coupling.json`.
- A test that cannot run on Windows is skipped explicitly, not silently.
- New tests must be deterministic; a flaky test is a bug.

---

## 18. Schedule, milestones, and staffing

### 18.1 Assumptions

- One primary maintainer with AI assistance; part-time review help.
- 20 weeks from 2026-11-02 to 2027-03-19.
- Two-week sprints; a milestone at the end of each.
- External dependencies (Apple account, Windows ARM64 runner, Flathub review) are not on the critical path for the code.

### 18.2 Milestones

| Milestone | Week | Date | Exit criteria |
|---|---:|---|---|
| M0: Baseline | 2 | 2026-11-13 | Baseline recorded; ADRs 0087–0090 accepted; spikes answered |
| M1: State v7 | 6 | 2026-12-11 | v7 migration, rollback, CLI, integrity pass |
| M2: API v2 | 8 | 2026-12-24 | Public v2-only; frontend/native hosts migrated; OpenAPI |
| M3: Portable library | 10 | 2027-01-08 | Move/rebase, media manifest, cross-platform tests |
| M4: Plugin + backup | 12 | 2027-01-22 | Plugin v2, backup v2, compatibility report |
| M5: Platform | 14 | 2027-02-05 | macOS notarized; ARM64 decision; Flathub-ready |
| M6: Feature complete | 16 | 2027-02-19 | All blockers green; docs drafted |
| M7: RC | 18 | 2027-03-05 | 72h soak; release notes; version sync |
| M8: Stable | 20 | 2027-03-19 | v2.0.0 published and verified |

### 18.3 Sprint plan

| Sprint | Weeks | Focus | Deliverables |
|---|---|---|---|
| S1 | 1–2 | Program setup | Branch, baseline, ADRs 0087–0090, spikes |
| S2 | 3–4 | State v7 core | Envelope, migration, runtime state, report |
| S3 | 5–6 | State v7 finish | CLI, fixtures, integrity, rollback, docs |
| S4 | 7–8 | API v2 | Route model, aliases, frontend, native hosts |
| S5 | 9–10 | Portable library | Paths, move wizard, media manifest |
| S6 | 11–12 | Plugin + backup v2 | Manifest, runner, services, backup format |
| S7 | 13–14 | Platform | macOS notary, ARM64, Flathub, reproducibility |
| S8 | 15–16 | Hardening | 50k spike, SQLite default, facet worker, coverage |
| S9 | 17–18 | RC | Docs, version sync, soak, release notes |
| S10 | 19–20 | Release | Tag, artifacts, verification, announcement |

### 18.4 Critical path

`W1 (state)` → `W3 (portable)` → `W6 (upgrade)` → `W7 (tests)` → `W8 (release)`.

`W2 (API)` can run in parallel with W1 after ADR 0091. `W4 (backup)` depends on W1 only for the state schema field. `W5 (platform)` is independent until the release job changes. `W9 (plugin)` is independent and can slip without blocking the release.

### 18.5 Schedule risks and cuts

If the schedule slips:

1. Cut Windows ARM64 (already conditional).
2. Cut Flathub submission (external anyway).
3. Cut plugin API v2 to 2.1.
4. Cut 50k exploratory work.
5. Never cut the migration, rollback, API v2, or backup v2.

### 18.6 Staffing

| Role | Allocation | Responsibilities |
|---|---|---|
| Maintainer | 1.0 FTE | Architecture, migration, API, release |
| Reviewer | 0.2 FTE | Security, C hosts, platform |
| Translator | 0.1 FTE | New locale keys |
| Release runner | CI | Builds, signs, attests |

---

## 19. Risk register

| ID | Risk | Likelihood | Impact | Mitigation | Trigger | Owner |
|---|---|---|---|---|---|---|
| R-01 | Migration corrupts a library | Low | Critical | v6 copy, atomic write, fault injection, 10k fixtures, soak | Any migration test failure | Maintainer |
| R-02 | Rollback loses post-migration edits | Medium | High | Explicit warning; `library.v7.kept.json`; UI confirmation | User clicks rollback | Maintainer |
| R-03 | API v2 breaks an external script | Medium | Medium | v1 bridge, deprecation headers, docs, 1.19.2 readiness | Issue report | Maintainer |
| R-04 | Frontend misses an endpoint | Medium | High | Endpoint contract test, grep gate, manual action matrix | Gate failure | Maintainer |
| R-05 | Native host/server mismatch | Medium | High | Version header, `HOST_API_UNSUPPORTED`, legacy aliases | Host smoke failure | Reviewer |
| R-06 | Portable token escape | Low | Critical | Containment, symlink checks, fuzz tests | Security review | Maintainer |
| R-07 | Move plan rewrites wrong paths | Medium | High | Plan ID, revalidation, preview/apply equality test | Stale plan test | Maintainer |
| R-08 | Backup v2 hash mismatch on a legitimate file | Low | Medium | Hash from the archive member, not the source; retry | Restore failure | Maintainer |
| R-09 | macOS notarization secrets unavailable | Medium | High | Ad-hoc fallback; release notes; 2.0.1 | Release dry run | Maintainer |
| R-10 | Windows ARM64 SDK lacks arm64 lib | Medium | Medium | Defer artifact; installer refusal stays | W0-05 spike | Reviewer |
| R-11 | Flathub review delays | High | Low | Submission-ready, not a blocker | External | Maintainer |
| R-12 | 50k write budget fails | High | Medium | Report honestly; keep 20k formal; 3.0 | W7-01 | Maintainer |
| R-13 | Coverage floor increase fails | Medium | Low | Raise after measurement; split tests; never lower | `make check` | Maintainer |
| R-14 | Plugin v2 scope creep | High | Medium | Freeze manifest; v1 adapter; slip to 2.1 | Sprint review | Maintainer |
| R-15 | i18n new keys incomplete | Medium | Low | English fallback documented; translate in 2.0.1 | Release review | Translator |
| R-16 | Docs drift | Medium | Medium | Version sync, changelog truth gate, docs review | `make version-check` | Maintainer |
| R-17 | Release key compromise | Low | Critical | Existing key hygiene; no new copies; rotate procedure documented | Security report | Maintainer |
| R-18 | Schedule slip | High | Medium | Cut list in §18.5 | M6 review | Maintainer |
| R-19 | Snap/flatpak/appimage behavior differs | Medium | Medium | Manual matrix; release smoke | RC soak | Reviewer |
| R-20 | User confusion on first launch | Medium | Medium | Wizard, report, docs, What's New | Beta feedback | Maintainer |

---

## 20. Release checklist

### 20.1 Pre-release verification

```bash
# Source and gates
make check
make contracts          # only after a deliberate surface change; review the diff
make version-check
./run_all_tests.sh
python3 -B scripts/check_api_contract.py
python3 -B scripts/check_api_paths.py
python3 -B scripts/check_openapi.py
python3 -B scripts/check_state_schema.py
python3 -B scripts/check_backup_format.py
python3 -B scripts/check_flat_imports.py
python3 -B scripts/check_macos_release.py
python3 -B scripts/check_reliability_catalog.py

# Performance
python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5
python3 -B scripts/perf_bench.py --sizes 50000 --no-gate   # exploratory only

# Packaging
bash build_appimage.sh
python3 -B tests/test_packaging.py
bash scripts/build_macos_app.sh
pwsh scripts/build_native_host_windows.ps1 -Architecture x64
pwsh scripts/build_native_host_windows.ps1 -Architecture arm64  # conditional
```

### 20.2 Upgrade rehearsal

For each platform:

1. Install 1.19.1 from its published artifact.
2. Create a library with 100 games, 5 plugins, 2 custom themes, a sync folder, and a backup.
3. Upgrade to the 2.0.0 RC.
4. Confirm the migration report, the compatibility report, and the library.
5. Launch a game, save a session, create a backup, restore a file.
6. Roll back; confirm the 1.19.1 library opens and the post-migration edits are gone.
7. Re-upgrade; confirm idempotence.

Record the results in `docs/archive/2.0-SOAK.md`.

### 20.3 Artifact verification

For every artifact:

- [ ] SHA-256 sidecar matches.
- [ ] Ed25519 signature verifies with `openbox-release.pub`.
- [ ] SBOM is attached.
- [ ] Provenance attestation is attached (where supported).
- [ ] The installer verifies the artifact on a clean machine.
- [ ] The app starts, shows the version, opens a library, and exits cleanly.
- [ ] macOS: `spctl --assess` passes; `stapler validate` passes.
- [ ] Windows ARM64: installs and launches on an ARM64 machine (conditional).
- [ ] Flatpak: installs and runs; `appstreamcli validate --no-net` passes.

### 20.4 Release notes

`docs/RELEASE_NOTES.md` must cover:

- What 2.0 is and why it is major.
- Upgrade instructions and the backup path.
- Rollback instructions.
- API v2 and the v1 bridge sunset.
- Plugin v2 and v1 compatibility.
- Backup v2 and the v1-compatible export.
- Portable library and its limits.
- macOS notarization status.
- Windows ARM64 status.
- Flathub status (submission-ready, not necessarily listed).
- Known limits (50k exploratory, i18n, no cloud account).
- Verification instructions (checksums/signatures).

### 20.5 Tag and publish

```bash
git tag -a v2.0.0 -m "OpenBox 2.0.0"
git push origin v2.0.0
```

The release workflow builds, signs, attests, and publishes. Verify the GitHub release from a clean machine before announcing.

### 20.6 Post-release

- Watch issues and crash reports for 14 days.
- Publish 2.0.1 for any release-blocking defect.
- Do not change formats in a patch.
- Start 2.1 planning: remove the v1 bridge, revisit Python 3.10, evaluate 50k.
- Archive this plan in `docs/archive/NEXT_UPDATE_PLAN-2.0.md` with an execution record.

### 20.7 Rollback of the release itself

If 2.0.0 is defective:

1. Publish 2.0.1 with the fix; do not attempt to unpublish 2.0.0.
2. Add the defect and the fix to the release notes.
3. If the defect corrupts data, publish a recovery guide and a 2.0.1 build that restores from `library.v6.backup.json`.
4. If the updater feeds on "latest", a 2.0.1 release is the rollback path; for a manual return to 1.19.1, restore `library.v6.backup.json` and reinstall the 1.19.1 artifact.
5. If the release key is compromised, rotate it and publish a new key anchor; that is a separate security incident.

---

## 21. Definition of done

### 21.1 Per pillar

**Portable library**

- [ ] `path_mode=portable` round-trips on Linux, Windows, and macOS.
- [ ] A moved root resolves all tokens; missing root gives a clear error.
- [ ] Token escape attempts are rejected.
- [ ] The move wizard preview equals the apply result or refuses.
- [ ] Media manifest verification reports missing/changed/untracked.
- [ ] Docs explain what moves and what does not.

**Contract reset**

- [ ] Public API is `/api/v2` only; v1 is off by default.
- [ ] Frontend and native hosts are migrated.
- [ ] OpenAPI lists every public route.
- [ ] Problem details are consistent.
- [ ] Flat parity imports are gone.
- [ ] Backup v2 and plugin v2 are documented.
- [ ] The v1 bridge is opt-in and has a sunset.

**Plugin API v2 (when shipped)**

- [ ] A v1 plugin runs unchanged.
- [ ] A v2 plugin runs through the line protocol with a bounded timeout.
- [ ] Host services are versioned and read-only.
- [ ] The sandbox status is truthful on Linux, macOS, and Windows.
- [ ] The compatibility report names every plugin and its disposition.

**Platform maturity**

- [ ] macOS stable artifact is notarized and stapled, or the release notes explicitly say it is not.
- [ ] Windows ARM64 artifact ships, or the installer still refuses clearly and 2.0.1 is planned.
- [ ] Flathub is submission-ready; submission is approved or explicitly deferred.
- [ ] Every artifact has a checksum, signature, SBOM, and provenance.
- [ ] Reproducibility report exists.

**Upgrade safety**

- [ ] Migration and rollback pass on 1/100/10k fixtures.
- [ ] The migration page handles every new error code.
- [ ] The compatibility report names plugins, themes, settings, sync, and backups.
- [ ] 1.19.2 (if shipped) detects v7 and refuses safely.
- [ ] The release notes explain the rollback.

### 21.2 Repository-level

- [ ] `make check` passes.
- [ ] `./run_all_tests.sh` passes on Linux, Windows, and macOS.
- [ ] Coverage floors are met or raised, never lowered.
- [ ] Every new runtime module is in `runtime_modules.txt`.
- [ ] Every new test is in `tests/` and runs standalone.
- [ ] Every new route is in `api_contracts.json` and `docs/openapi.json`.
- [ ] Every new setting is in `KNOWN_SETTINGS` and `scripts/contracts/settings.json`.
- [ ] Every new emulator definition key is present.
- [ ] Every `Tested` reliability row names a real test.
- [ ] Every user-visible change has a changelog entry.
- [ ] Every architectural change has an ADR.
- [ ] No generated file is committed by accident.
- [ ] No secret is committed.

---

## 22. Appendices

### A. Compatibility matrix

| Surface | 1.19.1 | 2.0.0 | 2.1.0 | 3.0.0 |
|---|---|---|---|---|
| `/api/v1` | On | Off by default; bridge opt-in | Removed | Removed |
| Unversioned public `/api/*` | On | Host/internal only | Removed | Removed |
| `/api/v2` | Partial | Complete public surface | Stable | Stable |
| `/api/host` | — | New | Stable | Stable |
| State schema 1–5 | Migrate | Migrate | Migrate | Read-only export |
| State schema 6 | Current | Migrate to 7 | Migrate | Read-only export |
| State schema 7 | — | Current | Current | Migrate to 8 |
| Plugin API v1 | On | Adapter | Adapter | Removed |
| Plugin API v2 | — | New | Stable | Stable |
| Backup v1 | Write | Read + compatible export | Read | Read |
| Backup v2 | — | Write/read | Current | Current |
| Sync format 3 | On | On | On | Migrate |
| Theme tokens | Stable | Stable + validation | Stable | Stable |
| Python 3.10 | Supported | Supported | Deprecated | Removed |
| macOS 11 | Supported | Supported | Supported | Supported |
| Windows x86_64 | Supported | Supported | Supported | Supported |
| Windows ARM64 | No | Conditional | Yes (if 2.0 deferred) | Yes |

### B. Task index

| Workstream | Tasks | Blockers |
|---|---|---|
| W0 | W0-01…W0-09 | M0 |
| W1 | W1-01…W1-12 | B-01, B-03 |
| W2 | W2-01…W2-12 | B-02, B-06, B-08, B-09 |
| W3 | W3-01…W3-10 | B-04 |
| W4 | W4-01…W4-06 | B-05 |
| W5 | W5-01…W5-09 | B-06, B-11, B-12 |
| W6 | W6-01…W6-08 | Upgrade |
| W7 | W7-01…W7-10 | All |
| W8 | W8-01…W8-08 | Release |
| W9 | W9-01…W9-08 | Plugin API v2 (parallel; not a blocker) |

### C. File change map

| Area | Files |
|---|---|
| State | `state_store.py`, `openbox.py`, `pkg/state/paths.py`, `pkg/state/cache.py`, `pkg/state/sqlite_readmodel.py` |
| API | `routes.py`, `routes/registry.py`, `api_errors.py`, `static/util.js`, `web_app.py`, `scripts/gen_api_docs.py` |
| Native hosts | `native_host.c`, `native_host_win.c`, `native_host_mac.m`, `scripts/build_native_host_windows.ps1` |
| Portable | `settings_schema.py`, `handlers/library.py`, `handlers/media.py`, `pkg/state/paths.py`, `pkg/parity/parity_library_move.py`, `static/settings.js`, `index.html` |
| Migration | `web_app.py`, `handlers/setup.py`, `static/migrate.js`, `static/setup.js`, `locales/*.json` |
| Plugins | `plugins.py`, `plugin_runner.py`, `plugin_runner_v2.py`, `handlers/extensions.py` |
| Backup | `pkg/parity/parity_backup.py`, `handlers/health.py`, `static/sessions.js` |
| Compatibility | `pkg/parity/parity_compat.py`, `handlers/setup.py`, `static/settings.js` |
| Release | `.github/workflows/*.yml`, `scripts/build_macos_app.sh`, `scripts/install.ps1`, `updates.py`, `scripts/gen_sbom.py` |
| Docs | `README.md`, `docs/*.md`, `openbox.metainfo.xml`, `locales/*.json` |
| Gates | `scripts/check_tests.py`, `scripts/contracts/*`, `Makefile`, `.github/workflows/ci.yml` |

### D. Command reference

```bash
# Full gate
make check

# Ratchets only
make ratchets

# Regenerate contracts after a deliberate change
make contracts

# One test
make test-one TEST=tests/test_state_v7.py

# Windows suite
python -B scripts/run_windows_tests.py

# Version surfaces
make version-check

# Performance
python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5

# Library tools
./openbox.sh --check-library
./openbox.sh --migrate-library
./openbox.sh --rollback-library

# Packaging
bash build_appimage.sh
bash scripts/build_macos_app.sh
pwsh scripts/build_native_host_windows.ps1 -Architecture x64
pwsh scripts/build_native_host_windows.ps1 -Architecture arm64

# Signatures
python3 scripts/verify_release.py --key openbox-release.pub <artifact>
```

### E. Open questions

1. Is the Apple Developer account available for notarization (D-01)?
2. Is a Windows ARM64 runner or VM available for smoke testing (D-02)?
3. Is Flathub submission authorized now, or is "submission-ready" the 2.0 goal (D-03)?
4. Should 2.0 ship a 1.19.2 preflight release (D-04)?
5. Is the 20-week schedule acceptable, or should the scope be cut to 16 weeks?
6. Should the v1 bridge exist at all, or is a clean break simpler (D-07)?
7. Is a 50k exploratory report useful, or should the team focus only on 20k (D-08)?
8. Does the project want a public OpenAPI document, given the API is local-only?
9. Should the migration report be included in diagnostics by default, or only on request?
10. Is the "Stable Ground" name acceptable?

### F. Glossary

| Term | Meaning |
|---|---|
| API v2 | The public `/api/v2` surface |
| Host channel | The `/api/host` surface used by native windows |
| State v7 | The `library.json` envelope and schema in 2.0 |
| Portable mode | `path_mode=portable`; paths under `library_root` become `@library/...` |
| v1 bridge | `OPENBOX_ENABLE_V1_API=1`; re-exposes v1 aliases through 2.0.x |
| Compatibility report | The first-run table of plugins, themes, settings, sync, and backups |
| Contract ratchet | A frozen baseline that may grow but only shrink with a retired ledger |
| Problem details | RFC 9457-style error envelope |
| Formal scale | The library size covered by blocking performance gates (20,000) |
| Exploratory scale | Measured but not gated (50,000) |

---

## Planning findings summary

| Finding | Evidence | Planned resolution |
|---|---|---|
| The public API is split across v1, v2, and unversioned paths | `routes.py`, `static/util.js`, native hosts | W2 makes `/api/v2` canonical and the host channel explicit |
| The flat `parity_*` bridge is still installed | `pkg/parity/__init__.py` | W2-04 removes it |
| The state format has no envelope or portable paths | `state_store.py` | W1 adds v7; W3 adds opt-in tokens |
| The backup manifest has no format version | `pkg/parity/parity_backup.py` | W4 adds v2 and v1 read |
| The SQLite read model is optional and rebuilds after writes | `pkg/state/cache.py` | W7-02 makes it default with a background rebuild |
| macOS is ad-hoc signed | `scripts/build_macos_app.sh` | W5-01 notarizes, with a documented fallback |
| Windows is x86_64 only | `scripts/install.ps1` | W5-03 adds ARM64 if the gates pass |
| Flathub is not submitted | `docs/flathub-checklist.md` | W5-05 makes it submission-ready |
| Formal scale is 20k | `docs/SUPPORT.md`, ADR 0032 | Kept; 50k is exploratory (W7-01) |
| The AppImage is glibc 2.35 | ADR 0065 | Kept; Python 3.10 stays for 2.0 |
| Plugin API v1 is frozen | ADR 0050 | W9 adds additive v2 |
| Theme tokens are stable | `static/app.css`, `scripts/check_tokens.py` | Kept; runtime validation added |

**End of plan.**
