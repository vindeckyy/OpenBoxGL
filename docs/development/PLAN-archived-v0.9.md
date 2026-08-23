> **Archived 2026-08-21.** This plan was implemented in v0.9.0-v1.0.0.

# OpenBox: full LaunchBox media catalog + native-feel window, completion and correction plan

Source feedback:

> It may sync with the LBDB, but it downloads maybe 1/6th of the metadata. No box backs, no discs/carts, no manuals, ads, etc. I don't see any option to add any of those... Retro games were absolutely built around the context and materials released at the time.
>
> Along with this, web apps simply will never feel as good as a native app... I also just dislike the idea of having to keep my personal Firefox instance with my everything on it open to play my games.

## State of the repo

Commit `8772835` (`feat: full LaunchBox media catalog and native app window`) is pushed and `origin/master` is in sync. All 39 test files pass plus the metadata self-tests. The commit shipped most of the prior plan: new media fields end-to-end, the `MEDIA_TYPE_MAP` downloader, UI checkboxes and selects, audit counters, app-window launch mode, and tests. It contains real gaps that this plan fixes.

## Verified ground facts

- The local LBDB (320MB, 184,790 games, 1,306,226 images) has 33 image types and no manuals of any kind.
- The app imports platforms as `NES`, `SNES`, `Game Boy`, `Game Boy Color`, `Game Boy Advance`, `Nintendo 64`, `Nintendo DS`, `Nintendo 3DS`, `GameCube`, `Wii`, `Wii U`, `WiiWare`, `Nintendo Switch`, `PSP`, `PlayStation`, `PlayStation 2`, `PlayStation 3`, `PlayStation Vita`, `Xbox`, `Xbox 360`, `Arcade`, `ScummVM`, `Sega Saturn`, `Disc image`, `PC` (`parity_import.py:20`, `openbox.py`).
- The LBDB spells the same platforms as `Nintendo Entertainment System`, `Super Nintendo Entertainment System`, `Nintendo Game Boy`, `Nintendo Game Boy Color`, `Nintendo Game Boy Advance`, `Nintendo 64`, `Nintendo DS`, `Nintendo 3DS`, `Nintendo GameCube`, `Nintendo Wii`, `Nintendo Wii U`, `Sony Playstation`, `Sony Playstation 2`, `Sony Playstation 3`, `Sony PSP`, `Sony Playstation Vita`, `Microsoft Xbox`, `Microsoft Xbox 360`, `Arcade`, `ScummVM`, `Sega Saturn`, `Windows`, `MS-DOS` (all verified present in the local DB). LBDB has no `Disc image`, `WiiWare`, `PC`, or `DOS` platform row, so those can never exact-match.
- `PLATFORM_ALIASES` (`metadata.py:45`) is keyed by `GB`, `GBC`, `GBA`, `PSX`, `PS1`, `PS2`, `PS3`, `PSP`, `XBOX`, `X360`, `DC`, `SATURN`, `WII`, `WIIU`, `3DS`, `DS`, `GAMECUBE`, `ARCADE`, `MS-DOS`, `DOS`. Only `NES`, `SNES`, `N64`, `Genesis`, `ARCADE`, `MS-DOS` can ever be hit by `search_games` today; the other 19 keys are dead.
- `search_games` (`metadata.py:159`) is the only alias consumer and ranks with `ORDER BY ... (lower(platform) = lower(?)) DESC`, so a correct mapping directly improves which LBDB result the user is offered for an imported game.
- `apply_game_metadata` (`metadata.py:185`) treats `manual` as an empty tuple and skips it. `manual` is nonetheless selectable in the metadata dialog, bulk dialog, image groups, and auto-import settings, so users can check a box that downloads nothing.
- The extraction cache (`extract_game`, `archives.py:156`) is a complete plain-file directory: a `.complete` marker plus the extracted tree under `DATA.parent / "cache/archives"`, with the extractor already ignoring `*.pdf` when picking the launch file. The metadata endpoint (`/api/media`) serves any field path that exists on disk, so serving a manual PDF needs no special media plumbing, but PDFs need an in-browser viewer or open-in-app link, not an `<img>`.
- Coverage stats on `/api/metadata/status` (`web_app.py:1554`) expose only `games`, `matched_games`, `matched_ratio`, `with_cover`, `with_box_back`, `with_cart_front`, `with_disc`; no UI renders them.
- App-window mode shipped as: `--app-window`/`--no-app-window` flags, `ui_window` setting (`app` default) validated in `save_settings` and `public_settings` (`web_app.py:2794`, `:453`), `resolve_app_window_browser` + `open_ui(native_window=...)` (`parity_gamescope.py:103`), a settings select at `index.html:774`, and tests. No docs site page, README mention, metainfo release entry, or Tk-launcher flag covers it. When a Chromium-family browser exists, `--app=` also fails, or the browser is missing, `open_ui` falls back to `xdg-open`, which reopens the user's personal browser: the exact thing the feedback complains about.

## Part 1: fix the platform alias table

### 1.1 Rewrite `PLATFORM_ALIASES` keyed by the app's actual platform names

In `metadata.py`, replace the current `PLATFORM_ALIASES` dict with one keyed by the exact strings the app stores in `game["platform"]` (`parity_import.py` `PLATFORM_BY_EXTENSION_EXTRA`, `openbox.py` `PLATFORM_BY_EXTENSION`, storefront importers). Value is the LBDB platform string verified to exist in the local `launchbox.db`. Target table, with reasons:

- `NES` -> `Nintendo Entertainment System`
- `SNES` -> `Super Nintendo Entertainment System`
- `Game Boy` -> `Nintendo Game Boy`
- `Game Boy Color` -> `Nintendo Game Boy Color`
- `Game Boy Advance` -> `Nintendo Game Boy Advance`
- `Nintendo 64` -> `Nintendo 64` (already exact; keep as identity or drop)
- `Nintendo DS` -> `Nintendo DS`
- `Nintendo 3DS` -> `Nintendo 3DS`
- `GameCube` -> `Nintendo GameCube`
- `Wii` -> `Nintendo Wii`
- `Wii U` -> `Nintendo Wii U`
- `Nintendo Switch` -> `Nintendo Switch`
- `PlayStation` -> `Sony Playstation`
- `PlayStation 2` -> `Sony Playstation 2`
- `PlayStation 3` -> `Sony Playstation 3`
- `PSP` -> `Sony PSP`
- `PlayStation Vita` -> `Sony Playstation Vita`
- `Xbox` -> `Microsoft Xbox`
- `Xbox 360` -> `Microsoft Xbox 360`
- `Genesis` -> `Sega Genesis`
- `Sega Saturn` -> `Sega Saturn`
- `Arcade` -> `Arcade`
- `ScummVM` -> `ScummVM`
- `PC` -> `Windows`
- `MS-DOS` -> `MS-DOS`
- `DOS` -> `MS-DOS`

Do not add entries for `WiiWare`, `Disc image`, or any other app name with no LBDB platform row; `PLATFORM_ALIASES.get(platform, platform)` already passes unknown names through unchanged, and a wrong alias would actively mis-rank.

Comment the table: keys are the app's own platform names, values are the LBDB spelling, used only for search ranking, and names not in the table pass through unchanged.

### 1.2 Extend `test_platform_aliases` in `test_metadata.py`

The current test only covers `SNES`. Extend it to build a small synthetic database with one game per platform and assert that `search_games(database, title, app_name)[0]["database_id"]` returns the exact-platform game for every key in the new table, including `Game Boy` (the current dead-code case) and `PlayStation` (`Sony Playstation` spelling). Also assert an unmapped platform such as `WiiWare` still returns results by title only, proving pass-through works.

## Part 2: give `manual` a real behavior

### 2.1 Extend the metadata pipeline with a manual finder

Add to `metadata.py`:

- `MANUAL_SUFFIXES = (".pdf", ".txt")` (module constant). PDF is the primary material; `.txt` is a harmless fallback for text manuals.
- `def find_archive_manual(game, media_root, opener=urlopen)`:
  - Returns `None` immediately when `game` has no `path` or `Path(game["path"]).is_file()` is false.
  - Skips unless `Path(game["path"]).suffix.lower()` is one of the archive extensions the app already handles (`{".zip", ".7z", ".rar"}`).
  - Uses `extract_game` from `archives.py` with the existing cache root. Import lazily inside the function (`from archives import extract_game`) to mirror the existing `from parity_media import ...` lazy-import pattern and avoid import cycles.
  - Walks the extraction root with `rglob("*")`, keeping regular files whose `suffix.lower()` is in `MANUAL_SUFFIXES` and whose name is not `.complete`.
  - Ranks candidates: exact `manual.pdf` first, then `manual.txt`; otherwise shortest name, then most recent `stat().st_mtime`; cap the candidate list at 8 to avoid pathological archives.
  - Copies the winner with `shutil.copy2` into `media_root/<database_id>/manual<suffix>` (parent dir created), returns the destination path string.
  - Wraps all extraction failures (`OSError`, `ValueError`, the existing `RuntimeError` the extractor raises on bad archives) in `try/except` returning `None`, so a bad archive never blocks metadata sync.
  - `manual` then keeps `MEDIA_TYPE_MAP["manual"] = ()` because no LBDB type exists, and the downloader loop delegates to the finder.

### 2.2 Wire it into `apply_game_metadata`

In the per-type loop in `apply_game_metadata`, replace the current `if media_type == "manual": continue` with:

- If `overwrite` or `not game.get("manual")`:
  - `candidate = find_archive_manual(game, root, opener)`
  - `if candidate: game["manual"] = candidate`
- When `manual` was requested but nothing was found, record a per-game note in the return value so the UI can show it. Add a new key to the returned dict that callers already tolerate, since `apply_metadata` in `web_app.py` computes `changes` by diffing the dict: append a non-field key such as `game["_media_notes"] = game.get("_media_notes", []) + [...]`. It survives the diff because it is new or appended, gets persisted via `transact_state`, and `_build_public_state` drops it because it is not in `FIELDS`. Keep the note text short: `"manual: no manual in this archive"`.

Do not touch the LBDB types; `MEDIA_TYPE_MAP["manual"] = ()` stays and gets a comment pointing at `find_archive_manual`.

### 2.3 Extend `test_metadata.py`

- Fixture: a `TemporaryDirectory` with a real `manual.pdf` and a game whose `path` is a zip containing `manual.pdf`, plus the existing `ImageResponse` opener. Assert `apply_game_metadata(..., ["manual"], ...)` sets `game["manual"]` to an existing file ending in `.pdf` inside `media/<database_id>/`.
- Assert the same call on a game whose zip has no manual leaves `"manual"` unset and adds the `_media_notes` entry.
- Assert `MEDIA_TYPE_MAP["manual"] == ()` stays, so the LBDB-less design is locked in.

## Part 3: render coverage stats and per-game feedback

### 3.1 Complete the backend coverage object

Extend the `coverage` dict in the `/api/metadata/status` handler (`web_app.py:1554`) to one `with_<field>` entry per media field, computed once over a shared field list so the endpoint stays one loop. Use the same `_missing` helper for every field including `manual`, `advertisement`, `title_screen`, `clear_logo`, `fanart`, `banner`, `icon`, `box_spine`, `box_3d`, `cart_back`, and `background` so the JSON is complete even if the UI only renders a subset at first.

### 3.2 Render coverage in the metadata dialog

In `index.html`, extend `renderMetadataStatus(status)` to display coverage facts under the existing status line when `status.ready` and `status.coverage` exist, using the existing `fact()` helper and the same warm-gold UI pattern as the media audit. Layout: a `<div class="facts" id="metadataCoverage">` inside the LaunchBox Games Database dialog, showing games, matched, matched ratio, and `with_cover` / `with_box_back` / `with_cart_front` / `with_disc` at minimum, plus `with_manual` so manual coverage is visible. Guard every value with `status.coverage &&` so an older server response degrades to today's layout.

### 3.3 Show the manual note after apply and bulk

- In `applyMetadata` (the JS function that calls `/api/metadata/apply`), after success, read the note from the returned payload if the backend echoes it: extend the handler in `web_app.py:2560` to include `"notes": updated.get("_media_notes", [])` in the JSON response. If notes exist, surface them through the existing `notify()` as a single line (`"No manual found in this game's archive."`). Keep the `_media_notes` field out of the public game state (already excluded by `FIELDS`).
- In the bulk flow, per-game notes cannot fit a toast; instead accumulate the note count in the bulk worker and expose it in the existing `MEDIA_JOB` result object, then render one line in `renderBulkMediaStatus` when the job is done: `"N games had no manual in their archive."` (wording decided at implementation; keep it one sentence).

## Part 4: serve and view manuals

### 4.1 Manual rendering in the UI

- In `renderArtwork`, handle `manual` specially: when `game.has_manual`, emit a button that opens `/api/document`-style behavior rather than an `<img>`. Concretely, add `data-manual="${media(game,'manual')}"` on a button labelled `Manual`, and an `openManual` handler that opens the media URL in a new tab (browser PDF viewer) so the PDF renders correctly. For `.txt` manuals, the same handler works because the browser renders text files natively. This avoids shipping a PDF.js dependency.
- Keep `manual` in `artworkKinds` for the gallery grid but replace the `<img>` for the manual entry with a placeholder tile (the game name or a `PDF` badge) that triggers `openManual`; do not attempt to load a PDF into an `<img>`.

### 4.2 Test coverage

Add a `test_parity_api.py` case that a game with a real `manual` PDF path returns `has_manual: true` from the state view and that `/api/media?kind=manual` serves the file bytes (the existing `/api/media` whitelist already includes `manual`; the test locks that in).

## Part 5: native window polish

### 5.1 Fix the settings copy

Superseded by the native-first rewrite: `index.html` is now a ~500-line shell and no longer carries an "Open the UI in" setting (the old `ui_window` key and the copy at the old `index.html:774` were removed). The current flow (`web_app.py:391-406`, `openbox.sh`, `openbox-native.sh`) is: `openbox` launches the WebKitGTK native host, which renders the one UI in a native window and falls back to the system-browser app window when the host is missing or fails; `openbox --web` opts out to the loopback web UI in a browser; and `open_ui` falls back to `xdg-open`, then the default browser, when no compatible browser can open a chrome-less window. Any replacement settings copy should read: "Applies the next time OpenBox launches. Falls back to your default browser when no compatible browser can open a chrome-less window."

### 5.2 Add `--window`/`--no-window` passthrough in `openbox.sh` and `openbox-native.sh`

Both scripts already pass `"$@"` to `web_app.py`, and `web_app.py` already parses `--app-window`/`--no-app-window` (`web_app.py:393-396`), so the flags are reachable from the shipped launcher entrypoints. The Tk launcher no longer exists: `openbox.py` is now the shared core (data paths, state store, launch commands) used by both the server and the native host, and the metainfo 1.0.0 release entry documents the native window default and its fallback instead.

### 5.3 Docs

- README: add one line under the launch section describing the app window default and the `ui_window` setting / flags. Done in the native-first refresh: the Quick Start and launch sections document the native WebKitGTK window default, the chrome-less app-window/browser fallback, and the `--web` opt-out; the `ui_window` setting itself was removed.
- Docs site `content/docs/reference/project/releasing.md` is release-only; the `ui_window` option no longer exists, so settings docs should describe the native window default and the `--web` opt-out instead, and the roadmap "In the current release" paragraph mentions the chrome-less app window and the expanded media types per the existing roadmap-update convention.

## Part 6: Verification

- `python3 -B test_metadata.py` prints both self-test ok lines, including the new alias, manual, and no-manual cases.
- `bash run_all_tests.sh` reports 39 test files, 0 failed (test count may rise if new files are added).
- A smoke run against the real DB: `python3 -B -c` snippet that loads the real `launchbox.db`, calls `search_games` with `("Game Boy", "Tetris")` and `("PlayStation", "Final Fantasy")`, and asserts the top result's platform equals the LBDB spelling. This proves the alias fix against the real 184k-game database.
- A manual smoke: create a temp zip containing `manual.pdf`, call `apply_game_metadata` with `["manual"]` and a stub game whose `path` is that zip, and confirm the manual file lands under `media/<id>/manual.pdf`.
- `python3 -m py_compile web_app.py metadata.py archives.py catalog.py parity_media.py parity_gamescope.py`.
- No version bump, tag, or release run; commit only. Release procedure stays in the openbox-github-release skill for when the user asks for a version.
