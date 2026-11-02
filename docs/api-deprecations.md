# Deprecations ledger

Every surface OpenBox has deprecated, who owns it, when it was announced, and when it is removed. `scripts/check_deprecations.py` fails when a row is incomplete, when a removal has already passed the current release, or when a removed surface is still live. ADR 0099 defines the ledger; ADR 0102 defines how 2.1 handles scheduled removals; ADR 0103 defines the API retirements.

| Surface | Owner | Announced | Default after announcement | Removed in | Sunset header | Status |
|---|---|---|---|---|---|---|
| `/api/v1/*` (61 frozen routes, `docs/archive/v1_contracts.json`) | Maintainer | 2.0.0 | Off; `OPENBOX_ENABLE_V1_API=1` restored it through 2.0.x. | 2.1.0 | `Mon, 19 Sep 2027 00:00:00 GMT` (historical) | Removed in 2.1.0 |
| Unversioned public `/api/*` (for example `/api/library`) | Maintainer | 2.0.0 | Private base paths only; `/api/v2/*` is the public surface. | 2.1.0 | None | Removed in 2.1.0 |
| Unversioned `/api/jobs` (1.19 job-map shape) | Maintainer | 2.0.0 | The jobs panel reads `/api/v2/jobs` list/cursor rows. | 2.1.0 | None | Removed in 2.1.0 |
| Native host legacy names: `/api/launch`, `/api/shutdown`, `/api/native/capabilities` | Maintainer | 2.0.0 | Hosts call `/api/host/*`; 2.0+ hosts keep working. | 2.1.0 | None | Removed in 2.1.0 |
| `OPENBOX_ENABLE_V1_API` | Maintainer | 2.0.0 | Ignored; a startup warning points to `/api/v2`. | 2.1.0 | None | Removed in 2.1.0 |
| 1.19-compatible backup write (format 1) | Maintainer | 2.0.0 | 2.0.x writes it; 2.1 reads format 1 but writes format 2 only. | 2.1.0 | None | Removed in 2.1.0 |
| Python 3.10 runtime support | Maintainer | 2.1.0 | Supported with a deprecation warning; 3.12 is recommended. | 3.0.0 | None | Deprecated, removal 3.0.0 |
| `openbox --rollback-library` | Maintainer | 2.1.0 | Still restores the v6 copy; keep `library.v6.backup.json`. | 3.0.0 | None | Deprecated, removal 3.0.0 |
| Plugin API v1 adapter | Maintainer | 2.0.0 | v1 plugins still run unchanged; v2 is the current API. | 3.0.0 | None | Deprecated, removal 3.0.0 |

## Rules

- A row needs an owner, an announcement version, and a removal version.
- The removal version is always later than the announcement version.
- A row marked `Removed` must not be live in the current build. A row marked `Deprecated` must not be past its removal version.
- A surface is removed only in the release named here, with a migration note in `docs/RELEASE_NOTES.md`.
- The historical `/api/v1` sunset date is kept in this table and `docs/archive/v1_contracts.json`; the 2.1 build no longer advertises a sunset header because the surface is gone.
