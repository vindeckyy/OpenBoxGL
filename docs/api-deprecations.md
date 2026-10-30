# API deprecations ledger

Every surface OpenBox has deprecated, who owns it, when it was announced, and when it is removed. `scripts/check_deprecations.py` fails when a row is incomplete, when a removal has already passed the current major line, or when the `/api/v1` sunset header in the code disagrees with the ledger. ADR 0099 defines the ledger; ADR 0091 defines the surfaces.

| Surface | Owner | Announced | Default in 2.0 | Removed in | Sunset header | Status |
|---|---|---|---|---|---|---|
| `/api/v1/*` (61 frozen routes, `docs/archive/v1_contracts.json`) | Maintainer | 2.0.0 | Off. `OPENBOX_ENABLE_V1_API=1` restores it; responses carry `Deprecation` and `Sunset`. Off returns `410 V1_API_DISABLED`. | 2.1.0 | `Mon, 19 Sep 2027 00:00:00 GMT` | Deprecated, bridge only |
| Unversioned public `/api/*` (for example `/api/library`) | Maintainer | 2.0.0 | Still routable as an internal layer through 2.0.x. Not documented and not in `docs/openapi.json`. Use `/api/v2/*`. | 2.1.0 | None | Deprecated, internal only |
| Unversioned `/api/jobs` (1.19 job-map shape read by the jobs panel) | Maintainer | 2.0.0 | Served unversioned. `/api/v2/jobs` is a list with cursors and is a different contract. | 2.1.0 | None | Deprecated, UI still reads it |
| Native host legacy names: `/api/launch`, `/api/shutdown`, `/api/native/capabilities`, `/api/v2/resume` as host calls | Maintainer | 2.0.0 | Still served. The three native hosts call `/api/host/*`. | 2.1.0 | None | Deprecated, served for old hosts |

## Rules

- A row needs an owner, an announcement version, and a removal version.
- The removal version is always later than the announcement version.
- A surface is removed only in the release named here, with a migration note in `docs/RELEASE_NOTES.md`.
- A change to the sunset date changes the `V1_SUNSET` constant in `routes.py` and this table in the same commit.
