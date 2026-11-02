# Settings retirement audit (2.0, plan W1-09)

Date: 2026-10-29. Command: `python3 -B scripts/audit_settings.py` (`--json` for the rows). For each key in `settings_schema.KNOWN_SETTINGS`, the script counts the source files that read or write it. It excludes the schema, the frozen contract, the generated docs, and the tests.

## Result

| Measure | Value |
|---|---:|
| Known settings | 153 |
| Keys with zero references | 0 |
| Keys with 1 or 2 references | 9 |
| `library_root` references | 37 (`handlers/settings.py`, `pkg/state/library_v7.py`, `pkg/parity/parity_backup.py`, others) |
| `path_mode` references | 24 |
| `media_manifest_enabled` references | 7 (`handlers/settings.py`, `handlers/media.py`, `pkg/state/library_v7.py`, `scripts/api_schemas.py`) |

The nine low-reference keys are live. Each has a reader or writer in the files listed below.

| Key | References | Files |
|---|---:|---|
| `gamescope_guest` | 2 | `pkg/state/cache.py`, `static/app.js` |
| `gameyfin_password_set` | 1 | `pkg/state/cache.py` |
| `last_health_rescan` | 2 | `handlers/library_health.py`, `pkg/parity/parity_library_health.py` |
| `last_update_check` | 1 | `handlers/health.py` |
| `library_journal_enabled` | 2 | `pkg/parity/parity_library_sync.py` |
| `retroachievements_enabled` | 1 | `handlers/insights.py` |
| `steamgrid_key_configured` | 2 | `pkg/state/cache.py`, `static/settings.js` |
| `webhook_attempts` | 2 | `handlers/extensions.py` |
| `webhook_timeout` | 2 | `handlers/extensions.py` |

`media_manifest_enabled` now has a consumer in 2.1. It is read by `handlers/media.py` before recording a download or capture, and by `pkg/state/library_v7.py` before integrity verification or repair. When false, the integrity report says the manifest is disabled and no hashing is done.

## Decision

Section 4.5 of the 2.0 plan allows a key to be retired only when it has had no reader and no writer for at least two minor releases, verified by a grep gate and by git history. No key has zero references, so no key is a retirement candidate. The nine low-reference keys are live and stay.

Result for 2.0/2.1: no settings are removed. `scripts/contracts/settings.json` changes by additions only (`library_root`, `path_mode`, `media_manifest_enabled`). The ratchet (ADR 0060) fails any removal that lacks a retired-ledger entry.
