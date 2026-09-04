# OpenBox v1.7.2

## Localization

- Full **i18n system**: `data-i18n` attributes in `index.html`, `t(key)` in JS via `static/i18n.js`, JSON locale files for **English, Spanish, German, French, and Portuguese** in `locales/`.
- Settings → Interface language selector; switching re-translates without reload. `scripts/check_i18n.py` enforces 100% key coverage (ADR 0015).

## Scale Foundation

- Optional **SQLite read model** (`pkg/state/sqlite_readmodel.py`) behind `OPENBOX_ENABLE_SQLITE_READ=1`: FTS5 full-text search (LIKE fallback), indexed filtered queries, GROUP BY facets. Off by default, zero behavior change (ADR 0014).

## Deck Polish

- **Gamescope presets**: 8 profiles (Steam Deck, HD, 1080p, 1440p, 4K, integer, stretch, borderless); **MangoHud** overlay toggle; controller bench tab with live gamepad visualization (ADR 0016).

## Emulator Health

- **BIOS SHA1 drift detection** (`BIOS_SHA1_DRIFT`) in Launch Doctor; health badge tokens in `app.css` and all 5 themes; `GET /api/v2/emulators/registry?health=1` returns `bios_ok`/`firmware_ok`/`core_ok` (ADR 0018).

## Smart Collections & Backup Diff

- Visual chip builder for filter presets (`rules_to_chips()` / `chips_to_rules()`) (ADR 0020).
- **Backup diff API**: `GET /api/v2/backup/diff?archive=<name>` returns added/removed/changed IDs plus settings status (ADR 0019).

## Verification

- `make check`: lint, compile, 82 test files, coverage floors green.
- SBOM: `OpenBox-1.7.2-sbom.json` (CycloneDX 1.4)
- SHA-256: `c7e24418df978b218ce4da180d8a44016cac99b2bff804273c8c66f4168e97ad`
- Ed25519 signature: `OpenBox-x86_64.AppImage.sig` (verified against openbox-release.pub)

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.1...v1.7.2
