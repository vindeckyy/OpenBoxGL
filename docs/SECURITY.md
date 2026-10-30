# Security Policy

## Fixed in 1.16.0: what 1.15.x and earlier are exposed to

If you run 1.15.x or older, these are the security defects 1.16.0 closes. None is
backported, so upgrading is the fix.

- **Arbitrary `.json` read through a crafted `preview_id`.** Two code paths (the import
  setup preview and the Launch Doctor) joined the request's `preview_id` into a file path
  unchecked, so `../../…/settings` returned any JSON file on disk, including `settings.json`
  with provider credentials. Every `preview_id` now goes through one validator and a
  containment check.
- **Arbitrary file write through a game's `rom_name`.** Restoring a high-score bundle built
  the destination filename from `rom_name`, so a traversal string wrote outside the
  high-score folder. The name is reduced to a safe component and the destination is
  checked to stay inside it.
- **Plugin sandbox could read the library.** The bubblewrap sandbox masked only `/home`,
  `/tmp`, `/run`, `/mnt` and `/media`. With `OPENBOX_DATA_DIR` (a supported override)
  pointing elsewhere, sandboxed plugins could read `library.json` and `settings.json`.
  The resolved data directory is now masked wherever it lives.
- **Unescaped strings in the Wrapped report.** Game names and an error message reached
  `innerHTML` without escaping. The CSP (`script-src 'self'`) blocked script execution, so
  this was one policy change away from exploitable rather than exploitable; it is escaped
  now and a source gate fails if a bare property reaches an HTML sink again.
- **A silently divergent JSON serializer.** When `orjson` happened to be installed, state
  writes used it and could produce different output from the standard library. It is
  removed; the runtime is standard-library only, enforced by `scripts/check_dependencies.py`.
- Smaller hardening: the RetroAchievements cache name is built from an integer, and the
  `flatpak info` probes have a timeout so a hung Flatpak cannot hang a request.

## Supported versions

| Version | Supported |
| --- | --- |
| 2.0.x | Yes (current) |
| 1.19.x | No — upgrade required |
| 1.18.x | No — upgrade required |
| 1.16.x | No — upgrade required (see above) |
| 1.15.x | No — upgrade required |
| 1.14.x | No — upgrade required |
| 1.13.x | No — upgrade required |
| 1.12.x | No — upgrade required |
| 1.11.x | No — upgrade required |
| 1.10.x | No — upgrade required |
| 1.9.x | No — upgrade required |
| 1.8.x | No — upgrade required |
| 1.7.x | No — upgrade required |
| 1.6.x | No — upgrade required |
| 1.5.x | No — upgrade required |
| 1.4.x | No — upgrade required |
| 1.3.x | No — upgrade required |
| 1.2.x | No — upgrade required |
| 1.1.x | No — upgrade required |
| 1.0.x | No — upgrade required |
| 0.9.x | No — upgrade required |
| 0.8.x | No — upgrade required |
| 0.7.x | Best effort |
| 0.6.x | Best effort |
| 0.5.x | Best effort |
| 0.4.x | Best effort |
| < 0.4.0 | No |

Security fixes are provided for the latest release on the `master` branch. Fixes land on `master` going forward; older lines are not backported — upgrade to the latest release to receive them. The 1.19.x line is the current maintained release; the older rows document the historical support policy and should not be read as promises of backported fixes.

## Reporting a vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Instead, report them privately using one of the following methods:

1. Open a **private security advisory** on GitHub: [Create a security advisory](https://github.com/vindeckyy/OpenBoxGL/security/advisories/new)
2. If GitHub advisories are unavailable, open a minimal public issue asking for a private contact channel without disclosing exploit details

Include as much of the following as possible:

- Description of the issue and potential impact
- Steps to reproduce
- Affected version(s)
- Proof of concept, if available
- Suggested remediation, if known

## Response expectations

Maintainers aim to acknowledge valid reports within **5 business days** and provide a remediation plan or status update within **14 business days**, depending on severity and complexity.

## Scope

The following are in scope:

- Remote code execution or privilege escalation in OpenBox server components
- Authentication or authorization bypass in the local web API
- Unsafe command execution introduced by OpenBox launch logic
- Credential leakage through logs, backups, or repository defaults

The following are generally out of scope:

- Vulnerabilities in third-party games, emulators, storefront clients, or operating system components launched by OpenBox
- Issues requiring physical access to an unlocked machine with an already running OpenBox instance
- Social engineering or phishing unrelated to OpenBox itself

## Safe usage guidance

- Run OpenBox on trusted local networks. The web UI uses a session token; do not expose it directly to the public internet without a reverse proxy and additional hardening.
- Do not commit `.env`, API tokens, RetroAchievements credentials, or EmuMovies credentials to the repository.
- Keep OpenBox updated to the latest release.
- Plugins run with an OS sandbox when bubblewrap is available. If bubblewrap is unavailable, OpenBox skips enabled plugins unless you trust that exact package in the Plugins manager (which then runs it unsandboxed) or `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` is set. Setting `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` disables the sandbox entirely, even when bubblewrap is available. Only use that override or trust for plugins you trust. macOS has no bubblewrap, so enabled plugins are skipped there unless the exact package is trusted.
- Integration tokens for IGDB, ScreenScraper, SteamGridDB, Gameyfin, and RetroAchievements are stored outside the public settings projection and returned only masked. Plugin capability credentials live in a separate store that backups exclude, and password-format plugin settings are blanked in backups.

## Release signing

Release publication is fail-closed. The workflow requires `OPENBOX_SIGNING_KEY`, derives an Ed25519 public key in a mode-`0600` temporary file, compares it with the committed `openbox-release.pub`, signs every published binary — the x86_64 and aarch64 AppImages, the Windows portable zip, the standalone Windows native-host executable, the macOS app bundle, and the emulator definition pack — and verifies each signature before upload. The updater and `scripts/install.sh` reject checksum-only releases.

The macOS bundle is ad-hoc signed rather than notarized: it carries no Developer ID signature, so macOS asks the user to allow it the first time it is opened. The release signature above is what the installer (`scripts/install-macos.sh`) and the in-app updater verify; the ad-hoc code signature only proves the bundle was not modified after it was built.

The committed `openbox-release.pub` pins the production release key. Maintainers must preserve these external release controls:

- Generate a new Ed25519 key pair outside the repository and store only the private key as the `OPENBOX_SIGNING_KEY` repository secret.
- When rotating the signing key, replace `openbox-release.pub` and update `RELEASE_KEY_SHA256` in `scripts/install.sh` in the same reviewed change.
- Require approval for the `release` environment and protect `v*` tags against unreviewed pushes; use annotated, signed tags where the GitHub organization supports the signing-key policy.
- Publish `openbox-release.pub`, the `.sig`, the checksum, and `install.sh` together. Rotate the key and repeat the pin update if it is compromised.

## Disclosure

We prefer coordinated disclosure. Reporters will be credited in release notes when fixes ship, unless they request anonymity.

## 2.0 threat-model addendum (W0-09, W7-07)

This addendum lists what 2.0 adds to the attack surface, the control for each item, and the test that exercises it. Gaps are listed separately and are not claimed as controls.

| Surface added or changed in 2.0 | Threat | Control | Test |
|---|---|---|---|
| Portable library tokens (`@library/...`) | A stored token escapes the library folder (`..`, drive prefix, leading separator, NUL, alternate data stream) | Tokens are refused at decode; containment is checked after normalization | `tests/test_portable_paths.py` |
| Portable library symlinks | A symlink inside the folder points outside it and is tokenized | A path that resolves outside the folder is not tokenized and is reported as `game_path_outside_root` | `tests/test_portable_library.py`, `tests/test_symlink_refusals.py` |
| Extra-app paths (`applications`, `versions`, `documents`) | The same escape and rebase risks as game paths | The same token rules; a move rebases each path slot by its own key | `tests/test_portable_library.py` (`ExtraAppPathTests`) |
| Move wizard | A stale preview applies to a library that has changed | Apply recomputes the plan under the store lock and refuses with `MOVE_PLAN_STALE` | `tests/test_library_move_http.py` |
| Library migration v6 to v7 | A crash or full disk leaves a truncated library | Backup written and fsynced first; primary replaced atomically; a failed step leaves the original file | `tests/test_state_migration_rollback.py` |
| Rollback | Rollback writes under a running server | Rollback refuses while a server answers on the recorded port | `tests/test_state_migration_rollback.py` |
| Library format newer than this build | An older build rewrites a newer library | Refused with `STATE_FUTURE_VERSION` without rewriting the file | `tests/test_state_v7.py` |
| Backup restore (format 2) | A tampered or unlisted member is written | Every member's size and SHA-256 are checked before any write; unlisted and missing members refuse the archive | `tests/test_backup_format_v2.py` |
| Backup archives | Path traversal and symlink members | Entries are validated before extraction; symlinks are refused | `tests/test_path_traversal.py`, `tests/test_symlink_refusals.py` |
| Backup format above this build | Silent misread of a newer archive | Refused with `BACKUP_FORMAT_UNSUPPORTED`; nothing is written | `tests/test_backup_format_v2.py` |
| Media manifest | A changed or substituted media file goes unnoticed | Size and mtime are checked on demand; a change triggers a hash comparison; untracked files are reported | `tests/test_media_manifest.py` |
| Host channel | A host built for another major API drives the server | `X-OpenBox-API-Version` is checked; a mismatch is refused with `HOST_API_UNSUPPORTED` before routing | `tests/test_api_v2_contract.py` |
| v1 API bridge | Retired unversioned routes return to the public surface | Off by default; `OPENBOX_ENABLE_V1_API=1` re-enables it | `tests/test_api_v2_contract.py` |
| Frontend requests | A request reaches an unversioned or unknown endpoint | Every `api()` call site must resolve to a live route; `/api/v1` may not appear in the frontend | `tests/test_frontend_endpoint_contract.py` |
| Failure messages | A code is shown raw or not at all | Five codes map to explanations; a test requires an English explanation for each | `tests/test_api_problem_details.py` |
| macOS bundle | An ad-hoc build is described as notarized | Notarization is claimed only with `spctl` evidence; the entitlements file is empty by default; the gate checks the pipeline | `tests/test_macos_release.py`, `docs/archive/HANDOFF-MACOS-NOTARY.md` |

Gaps (not controls):

- The Linux and Windows native hosts send `X-OpenBox-API-Version`, but they are compiled only in CI. Compilation was not run on this workstation.
- The macOS bundle is ad-hoc signed, not notarized. The maintainer decided 2.0 ships that way, so the Privacy & Security confirmation on first launch still applies (ADR 0094)ed.
- Plugin API v2 is implemented (ADR 0092). A v1 plugin keeps the v1 runner and the v1 payloads; a v2 plugin runs under the JSON Lines runner with an output cap.
- The pre-app migration page (W1-12) is implemented: the upgrade is reported before the library is used, and the rollback restores the copy of the old file.

