# ADR 0080: Durable media download queue

**Date:** 2026-10-19
**Status:** Accepted (1.18.0)

## Context

Media downloads had two shapes. Bulk media kept its progress in the in-memory `MEDIA_JOB` worker, and imports ran
provider downloads as part of the import that discovered the games. Provider artwork is requested per
`(game_id, source, kind)` and fails transiently for reasons the library cannot control (missing credentials, an
unreachable provider, a game with no matching asset). A restart lost queued work, and every import path needed its own
download policy.

ADR 0011 already owns operation records and their lifecycle. Its unit is a user-visible operation with a phase and a
checkpoint, not an individual asset that may be retried for days; storing thousands of per-asset retries in
`operations.json` would fight that retention model.

## Decision

### Storage and identity

- The queue is one owner-only JSON list at `<data>/media-queue.json`, written atomically with mode `0600` and mutated
  under a sidecar file lock (`.media-queue.json.lock`). It is durable state, not a job record.
- An item is keyed by `(game_id, source, kind)`. Its id is `mq-<first 12 hex of SHA-1(key)>`, so a re-enqueue is
  idempotent.
- Each item carries `game_id`, `name`, `path`, `source`, `kind`, `media` (the kinds still to fetch), `url`, `overwrite`,
  `state`, `attempts`, `last_error` (truncated to 500 characters), and `next_attempt_at`.
- A job that lists several kinds (`media: [...]`, the legacy import shape) is split into one item per kind, because
  retry, skip-existing, and limits operate per asset. Duplicate keys replace the existing item unless the
  `overwrite` flag differs, in which case both entries are kept.
- The queue holds at most 5,000 items (`MEDIA_QUEUE_MAX`). Enqueueing beyond that raises `ValueError` rather than silently
  dropping work; the import helper logs it and stops feeding that batch, and a direct bulk enqueue reports the error to its
  caller. Entries that fail normalization on read (such as an unsupported `source`) are ignored.

### Sources and kinds

- Built-in sources are `launchbox`, `screenscraper`, `igdb`, `steamgriddb`, and `emumovies`. A plugin source is `plugin:<id>`.
  Any other source is rejected.
- An item without a source is inferred: `launchbox_db_id` gives `launchbox`, a truthy `emumovies` key gives `emumovies`,
  `steamgrid_id` or `steamgriddb_id` gives `steamgriddb`, and anything else gives `launchbox`.
- A kind must be in `MEDIA_TYPES_ALL` (`pkg/state/media_probe.py`) or be `video_recording`. In the `launchbox` source,
  `poster` and `square` are fetched through the cover path.
- Providers: `launchbox` uses `metadata.apply_game_metadata`; `screenscraper`, `igdb`, and `steamgriddb` take the media their
  search candidates return; `plugin:<id>` downloads the item's `url`, or the plugin's first media candidate when no URL is set;
  `emumovies` uses `KIND_MAP` in `pkg/parity/parity_emumovies.py` and needs EmuMovies credentials.

### Processing

- States are `queued` and `failed`. A completed item is removed from the file. Failed items are skipped until the
  retry route resets them.
- A provider failure stops the item's remaining kinds for that pass. The item keeps the kinds not yet fetched, increments
  `attempts`, and retries after 5 seconds (first failure) or 30 seconds (second). On the third failure it becomes `failed`
  and keeps `last_error`. A game that is no longer in the library fails the item at once.
- Before downloading, an item is skipped when the target field is already populated and `overwrite` is false. The
  `manual` kind is never replaced, even with `overwrite`. Screenshot results append instead of replacing unless
  `overwrite` is set.
- `media_download_limit` (0–10,000, `0` = unlimited) caps the number of distinct games completed per processing run.
  Reaching it stops the run with the remaining items still queued. `media_download_limit_per_group` (0–100, `0` =
  unlimited) caps successes per `(platform, kind)`; when a group is exhausted, the item keeps its remaining kinds and
  stays queued while the run continues with the next item.
- Each applied path is committed in its own state update (`openbox.update_state`), so a later provider failure cannot roll
  back an earlier successful download.
- The worker thread (`start_media_queue_worker` in `pkg/state/imports.py`) starts at boot after the auto-import worker
  (`web_app.py`), after bulk enqueues and retries, and when resume finds it stopped. Pause, resume, and cancel are
  process-local events: pause stops before the next item, cancel stops the loop, and neither mutates the queue file.
  Progress is broadcast on SSE as `media-queue` `completed`, `failed`, and `done` events.

### HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/media/queue` | Snapshot: `items` (also `queue`), `running`, `paused`, `cancelled`, `completed`, `failed`, and `counts` (`total`, `queued`, `failed`, `completed`, `cancelled`) |
| POST | `/api/media/queue/pause` | Set the pause event and return the worker state |
| POST | `/api/media/queue/resume` | Clear pause/cancel, start the worker if needed |
| POST | `/api/media/queue/cancel` | Set the cancel event and return the worker state |
| POST | `/api/media/queue/retry` | Body `id`. Reset one item's `state`, `attempts`, `last_error`, and `next_attempt_at`, and start the worker if it is stopped |

### Ingress

Every entry point goes through `enqueue_media_job` (`pkg/parity/parity_media.py`).

- Imports (`enqueue_import_media` in `pkg/state/imports.py`): folder imports and merged storefront imports, including plugin
  storefronts. Only games with a LaunchBox id or a `metadata_provider` are queued. Kinds come from `auto_import_media_types`
  (default cover, background, and screenshots). `media_download_limit` caps the games queued per import batch. Enqueue
  failures are logged and never abort the import.
- Bulk (`POST /api/media/bulk`, `handlers/media.py`): games without a LaunchBox id whose `metadata_provider` is ScreenScraper,
  IGDB, SteamGridDB, or an enabled plugin, and games with a SteamGridDB id, are queued. LaunchBox games, and every game on a
  retry-failed run, stay on the `MEDIA_JOB` batch.
- Plugin metadata apply (`plugin_metadata_apply` in `pkg/parity/parity_plugin_providers.py`): queues up to 20 media URLs per
  applied candidate as `plugin:<id>` items with `url` set.
- EmuMovies: the worker processes `emumovies` items, but no in-app path creates them at 1.18.0. The manual EmuMovies route
  downloads synchronously.

## Alternatives considered

- **Store queue items as ADR 0011 operations.** Rejected: operations are user-visible, bounded-history records with
  phase checkpoints. Per-asset retries would be pruned by operation retention and would make the Activity Center the
  retry UI for an internal queue.
- **Keep the queue in memory.** Rejected: a restart would lose accepted work and imports would have to re-derive it.
- **One item per game.** Rejected: it cannot skip an existing kind, retry one provider independently, or honor a
  per-kind limit.
- **Only a global limit.** Rejected: one platform with missing artwork could consume the whole run.
- **Fail an item on the first error.** Rejected: provider and network failures are usually transient; three attempts
  with backoff distinguish a bad asset from a bad moment.
- **Write the whole batch in one transaction.** Rejected: a late provider failure would roll back media already
  downloaded and recorded.

## Evidence

`tests/test_parity_media_queue.py` covers legacy normalization, invalid sources, duplicate/overwrite replacement,
per-kind splitting, the queue cap, import ingress, the global and per-group limits, backoff and failed state, retry
reset, existing/manual media preservation, pause/cancel leaving the queue untouched, and the snapshot shape.
`tests/test_emumovies.py` covers the EmuMovies provider.

## Consequences

- A restart resumes queued items; pause and cancel do not survive a restart because they describe the worker, not the
  backlog.
- Failed items accumulate in the file until retried, so the failure list is the retry ledger.
- Limits are policy for one run, not a lifetime quota.
- Existing media is preserved by default; replacing it requires an explicit `overwrite`, except `manual`, which is
  never replaced.
- The LaunchBox metadata-database batch keeps its existing `MEDIA_JOB` progress surface; this queue governs provider and
  plugin asset downloads.
- Known limit: a run writes back the list it loaded when the run started. An item enqueued, or a retry applied, while that
  run is in progress can be dropped or reverted by the run's next write. Enqueue or retry it again after the run ends.
