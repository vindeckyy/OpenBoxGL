# OpenBox architecture

OpenBox is a local-first Linux game library and launcher with two UI shells over one filesystem-backed application core.

```text
openbox / AppImage
        |
        +--> web_app.py
        |      |
        |      +--> loopback HTTP server
        |      +--> browser UI in index.html
        |      +--> game processes and session lifecycle
        |      +--> import, metadata, backup, and integration jobs
        |
        +--> openbox.py
               |
               +--> lightweight native Tk UI

Both shells
        |
        +--> shared state and launch helpers
        +--> JsonStateStore
        +--> user data directory
```

## Architecture at a glance

The application keeps library data on local disk. The browser is a local API client, not the domain layer. Python owns persistence, imports, launch command construction, process sessions, integrations, plugins, and themes.

The main tradeoff is deliberate: JSON and a loopback server keep OpenBox portable, inspectable, and account-free, while the web UI provides the full feature surface. The cost is that the web UI is a large single HTML document and the native UI is a separate presentation stack.

## Two UI shells over one local core

The web UI is the primary full-featured interface. The native UI is a lightweight fallback for desktop use.

| Shell | Entry point | Role |
|---|---|---|
| Web | `web_app.py`, `index.html` | Full library management, REST API, Big Box, integrations |
| Native | `openbox.py` | Lightweight desktop launcher |

`build_appimage.sh` dispatches to `openbox.py` when `--native` is supplied and otherwise starts `web_app.py`. `openbox.sh` and `openbox-native.sh` are thin wrappers around those entry points.

The two shells share the state directory and important domain helpers from `openbox.py`, including `DATA`, `STATE_STORE`, `load_state()`, `update_state()`, `recover_state()`, `discover_profiles()`, and `build_launch()`. The native UI is not a web client, so feature parity requires maintaining both presentation layers.

## Browser bootstrap and loopback API

The web runtime starts in `web_app.main()`:

```text
web_app.main()
    -> bootstrap_env(DATA.parent)
    -> configure logging
    -> ensure stock themes
    -> submit auto-import worker
    -> create ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    -> write server.port and server.token
    -> build token-bearing localhost URL
    -> open the browser
    -> serve until shutdown
```

The server binds to loopback only and asks the operating system for an ephemeral port. A per-process token is written to owner-readable files under the data directory and included in the browser URL.

`Handler._do_GET()` serves `index.html`, favicon assets, theme CSS, media, and JSON endpoints. The browser-side `api()` helper sends the token as `X-OpenBox-Token`; resource URLs carry the token as a query parameter. The handler compares the token with `secrets.compare_digest()` before serving protected resources.

```text
index.html
    -> api(path, options)
    -> X-OpenBox-Token
    -> Handler.authorized()
    -> endpoint dispatch
    -> domain or integration module
    -> state-store transaction or filesystem operation
    -> JSON response
```

This is a local security boundary, not a multi-user authentication system. Anyone who obtains the live token can operate that local instance.

## The data directory is the application boundary

`openbox.py` chooses the data directory from `OPENBOX_DATA_DIR` or the default path:

```text
~/.local/share/openbox-game-launcher/
```

The directory contains the primary state and the files that make the local application work:

```text
APP_DIR/
├── library.json
├── library.json.bak
├── .library.json.lock
├── themes/
├── plugins/
├── media/
├── cache/
├── backups/
├── server.port
└── server.token
```

If the new data file is absent and the legacy LaunchBox Linux path exists, OpenBox copies the legacy library into the new directory once. This preserves existing libraries while establishing a project-owned location.

The local directory is the source of truth. Cloud sync, storefronts, metadata services, and webhooks are integrations around that source, not replacements for it.

## State schema, stable identity, and transactional writes

`state_store.py` owns schema normalization and persistence. The current schema version is 4. State contains games, profiles, history, settings, playlists, queue, and notifications. Migrations run in order, unknown fields are retained, malformed collection fields are repaired, and invalid state raises `StateCorruptError` rather than being silently discarded.

Game IDs are stable hashes derived from normalized identity fields such as path, platform, storefront IDs, ROM name, and metadata IDs:

```text
game identity fields
    -> normalized identity payload
    -> stable JSON representation
    -> SHA-256 prefix
    -> game-<digest>
```

Stable IDs allow playlists, history, sessions, and API calls to survive list reordering and deletion of neighboring games. A meaningful identity change can create a new ID, which is preferable to silently pointing at a different game.

`JsonStateStore.update()` protects writes with both an in-process reentrant lock and a filesystem lock. It reloads the current file, applies a mutation, normalizes state, writes through a temporary file, fsyncs, updates the backup, atomically replaces the primary file, and refreshes the cache.

```text
update(mutator)
    -> thread lock
    -> filesystem lock
    -> reload current state
    -> mutate and normalize
    -> atomic write and backup
    -> fsync file and directory
    -> update cache
```

JSON is intentionally inspectable and portable. The tradeoff is whole-document read/write cost as libraries grow, which is partly mitigated by signature-based caching and tested fast paths.

## Import normalization and adapter modules

The browser import controls call API endpoints such as `/api/import`, `/api/import/steam`, `/api/import/heroic`, `/api/import/lutris`, and `/api/import/arcade`.

The common folder path is handled by `import_folder_path()` in `web_app.py`:

```text
index.html importFolder()
    -> POST /api/import
    -> import_folder_path()
    -> parity_import.import_multi_platform()
       or emulator-choice import
    -> normalize candidates
    -> skip duplicate paths
    -> update_state()
    -> return additions and recommendations
```

Import adapters live in focused modules such as `importers.py`, `arcade.py`, `parity_import.py`, and `parity_premium.py`. They return normalized game dictionaries instead of owning persistence directly. The broad `FIELDS` set in `web_app.py` acts as a partial boundary for accepted game fields.

This keeps new sources additive and testable. The tradeoff is that the shared dictionary schema is broad and informal compared with a relational model.

## Launch command construction

The browser sends a stable game ID to `/api/launch`. `start_game()` resolves the record, selects the platform profile, calls `build_launch()`, runs launch plugins, starts the process, updates play metadata, and registers the session.

`build_launch()` in `openbox.py`:

- validates the configured path;
- optionally extracts an archive into the data-directory cache;
- selects a per-game command or platform profile;
- substitutes `{path}`, `{name}`, `{app_id}`, `{heroic_app_id}`, `{lutris_id}`, and `{rom_name}`;
- runs shell scripts through `bash` when needed;
- otherwise executes the configured path directly.

The flexible tokenized command model supports ROMs, storefronts, emulators, and standalone executables without requiring a separate launcher class for every platform. Its boundary is the validated command and path input.

## Session ownership and process-group control

The web process owns live session state in `RUNNING`, `PROCESSES`, and `SESSION_EVENTS`. `start_game()` launches with `start_new_session=True`, registers the process group, updates play count and progress, and publishes a session-start event.

A background `finish_session()` thread waits for the process, records exit code and duration, updates play time and history, restores performance settings, runs after-session plugins, removes live process state, and publishes the completion event.

The browser polls `/api/running?after=<event sequence>` to update running counts, history, lifecycle messages, and session controls.

```text
POST /api/launch
    -> start_game()
    -> resolve stable ID
    -> build_launch()
    -> before_launch plugins
    -> Popen(new process group)
    -> RUNNING / PROCESSES
    -> session.started
    -> finish_session()
    -> playtime and history update
    -> after_session plugins
    -> session.finished
```

Process groups make pause, resume, stop, restart, and force-close practical for emulator and game process trees. Live sessions are intentionally process-local, so an unexpected server exit cannot preserve all in-flight state.

## Plugins: JSON subprocess hooks

Plugins are user-owned Python packages under `APP_DIR/plugins/`. `plugins.py` validates `plugin.json`, restricts supported hooks, keeps entry files inside the package, and supports `library`, `before_launch`, and `after_session` hooks.

Runtime execution is bounded:

```text
OpenBox lifecycle event
    -> run_plugins(directory, hook, payload)
    -> validate enabled manifests
    -> start plugin_runner.py
    -> JSON over stdin/stdout
    -> enforce timeout and output limits
    -> validate result
    -> continue the main pipeline
```

The runner strips several environment variables, starts a separate process session, limits execution to five seconds, caps payload/output, logs failures, and keeps hook failures from normally stopping the application. Package installation uses staging, backup, atomic replacement, and rollback.

This is containment and fault isolation, not a security sandbox. Plugins execute with the local user's privileges. Hook payloads are dictionary contracts rather than a separately generated schema.

## Themes: CSS overlays with preservation semantics

Bundled themes come from `themes/*.css` and are installed by `stock_themes.ensure_stock_themes()`. User themes live under `APP_DIR/themes/` and are selected or imported through the web API.

The browser loads the active stylesheet dynamically through `/api/theme.css`. Themes override visual roles while preserving the shared HTML and interaction structure. The installer restores missing bundled defaults but does not overwrite user-imported themes or edited stock files.

CSS is a deliberately lightweight extension point. It can change presentation without granting code execution or requiring a second component renderer. It also cannot add behavior, which keeps the extension boundary clear.

## Concurrency model

OpenBox has several distinct concurrency layers:

- `ThreadingHTTPServer` handles browser requests in worker threads.
- `JsonStateStore` uses thread and filesystem locks for persistent writes.
- `STATE_LOCK`, `PROCESS_LOCK`, and related locks protect process-local runtime structures.
- Auto-import, metadata, media, and emulator operations use background jobs.
- Each launched game has a completion thread waiting on its process.

Persistent state is protected across the native and web shells. Live process state is owned by the running web process and is not a second durable database.

## Failure recovery and durability

The application treats local corruption, missing paths, unsafe archives, failed integrations, and process failures as expected boundary conditions.

- State writes are atomic and backed up.
- Schema migration is explicit and preserves unknown fields.
- Missing game paths fail before launch.
- Archive extraction checks containment and safe paths.
- Failed plugin updates roll back staged package changes.
- Session failures expose exit codes and launch-command guidance in the browser.
- Network and integration failures return readable API errors and toast messages.

The recovery model favors preserving local data and reporting the concrete failed boundary over silently continuing with partial state.

## Security boundaries and non-goals

The project threat model is local application safety, not hostile multi-tenant hosting.

Existing boundaries include:

- loopback-only HTTP binding;
- per-process token authorization;
- secure token file writes;
- path and symlink checks for documents, backups, media, and archives;
- bounded plugin subprocess execution;
- validated webhook destinations and secret handling;
- process-group control for launched sessions.

OpenBox does not provide user accounts, remote shared tenancy, or a security sandbox for trusted local plugins. Exposing the loopback server or sharing the token changes the trust model.

## Test contracts

The repository uses independent executable `test_*.py` files collected by `run_all_tests.sh`. Tests exercise real temporary files, subprocesses, migrations, locks, integrations, and feature modules.

Representative architectural contracts:

- `test_state_v4.py`, `test_perf_state.py`, and `test_perf_writes.py` cover schema migration, stable IDs, caching, and atomic writes.
- `test_sessions.py` covers pause, resume, stop, restart, cleanup, and stable-ID behavior after neighboring deletion.
- `test_plugins.py` covers hook execution, enable/disable, installation, and rollback.
- `test_stock_themes.py` covers bundled theme installation and user customization preservation.
- `test_backend_hardening.py`, `test_secrets.py`, and `test_packaging.py` cover boundary safety.

The tests are close to the modules they protect. That makes behavior easy to trace, while the tradeoff is limited formal API-contract tooling.

## Module ownership map

The repository is organized around capability boundaries rather than a framework package layout.

| Area | Main files | Owns |
|---|---|---|
| Web orchestration | `web_app.py`, `index.html` | HTTP dispatch, browser state, API workflows, dialogs, Big Box, lifecycle coordination |
| Shared launch and state entry points | `openbox.py` | Data path selection, state-store access, launch command construction, profile discovery, native Tk shell |
| Persistence | `state_store.py`, `backend_io.py` | Schema normalization, migrations, locks, atomic writes, recovery, bounded file operations |
| Catalog rules | `catalog.py`, `parity_filter_presets.py` | Progress values, tags, bulk edits, relationships, filter presets, facet queries |
| Import adapters | `importers.py`, `arcade.py`, `parity_import.py`, `parity_premium.py` | Source discovery and conversion into normalized game records |
| Launch integrations | `emulators.py`, `parity_gamescope.py`, `parity_tracking.py`, `parity_perf.py` | Emulator profiles, Game Mode behavior, process tracking, handheld performance settings |
| Media and metadata | `metadata.py`, `parity_media.py`, `parity_igdb.py`, `parity_integrations.py` | Database search, artwork, trailers, screenshots, media jobs, external metadata |
| User extensions | `plugins.py`, `plugin_runner.py`, `stock_themes.py`, `themes/` | Bounded Python hooks and CSS presentation overrides |
| Verification | `test_*.py`, `run_all_tests.sh` | Standalone feature and integration contracts |

`web_app.py` is the composition root for the web application. New domain behavior should live in a focused module when it has its own rules or external boundary. The handler should validate the request, call that module, persist through the shared state path, and return a small response. `index.html` should coordinate visible state and user feedback, not reimplement filesystem or process rules.

## Browser API conventions

The browser API is a local JSON protocol implemented by the `Handler` class in `web_app.py` and consumed by `api()` in `index.html`.

A new endpoint normally follows this path:

```text
index.html action
    -> api(path, {method, body})
    -> Handler route
    -> boundary validation
    -> focused domain helper
    -> update_state() or bounded filesystem operation
    -> send_json(status, payload)
    -> refresh, render, or notify in the browser
```

Keep endpoint behavior predictable:

- Use JSON objects for request and response bodies.
- Send the token through the existing authorization path.
- Validate paths, URLs, IDs, and external values at the handler boundary.
- Return the concrete failure text that helps the browser offer recovery.
- Use stable `game_id` values for game references. Treat list indexes as legacy compatibility only.
- Keep long work in `JobManager` or an existing background worker and expose job status to the browser.
- Update the UI after a successful mutation rather than assuming the in-memory browser copy is complete.

The browser's `api()` helper converts transport failures, invalid JSON, and non-2xx responses into `Error` objects. A feature should preserve that path instead of adding a second fetch wrapper.

## Browser state and rendering rules

`index.html` keeps the current library in memory through variables such as `games`, `playlists`, `appSettings`, `selectedId`, filter state, and running-session state. Rendering is split by responsibility:

- `refresh()` reloads the public library snapshot and settings.
- `render()` rebuilds sidebar, grid, and detail state.
- `renderGrid()` owns filtering, sorting, list/grid output, virtualization, selection, and empty states.
- `renderDetails()` owns the selected game inspector and its action wiring.
- Feature functions open dialogs and attach handlers for their own content.

When adding a UI feature, decide which state is durable, which state is temporary, and which render function owns the visible result. Durable values belong in the API and state store. Selection, open dialogs, search text, and job polling belong in browser state. Do not make a render function depend on a hidden side effect when the same state can be passed or read from its named owner.

The current grid uses a windowed slice for large collections. Any new row or card content must tolerate virtualization, re-rendering, long names, missing media, and selection changes. Attach event handlers after output is replaced, and avoid retaining references to removed DOM nodes.

## Lifecycle state model for features

Features that call an external service or start a process should expose a visible state path:

```text
idle
  -> loading or queued
  -> success
  -> error
  -> retry or cancel
```

The implementation already uses this model for metadata, media, emulator installation, storefront imports, running sessions, and launch lifecycle messages. Reuse the existing patterns:

- disable the triggering action while a duplicate request would be unsafe;
- keep the action footprint stable while it waits;
- put job progress or status text near the action;
- restore the action after success or failure;
- report the actual error returned by the backend;
- refresh durable state after a successful mutation;
- make cancellation or retry explicit when the operation supports it.

A toast is suitable for a short success or error confirmation. A dialog or inline status is better when the operator must make a decision or watch a long job. Do not leave a request in a blank waiting state.

## Adding a new feature safely

A feature that crosses the browser and Python layers should be implemented in this order:

1. Identify the durable state fields and migration impact in `state_store.py`.
2. Identify the domain operation and keep it in the nearest focused module.
3. Add the handler route and validate all boundary inputs.
4. Add a standalone test for the domain behavior and a handler or integration test when the path crosses process, filesystem, or HTTP boundaries.
5. Add the browser action, loading state, success state, error recovery, and empty state in `index.html`.
6. Refresh or rerender from the committed response.
7. Check mobile controls, keyboard focus, Escape behavior, and reduced motion.
8. Run the same standalone test files through `run_all_tests.sh`.

If the feature has a plugin or theme seam, add it after the core path works. Plugins should receive a bounded JSON payload and return a documented dictionary result. Themes should override existing visual roles rather than require new behavior.

## Change checklist by boundary

Before merging a cross-cutting change, check the boundary it touches:

- **State:** migration, defaults, unknown-field preservation, stable IDs, backup, concurrent writers.
- **Filesystem:** absolute paths, symlinks, containment, permissions, size limits, cleanup on failure.
- **Process:** command construction, executable validation, process group ownership, exit code, cleanup.
- **HTTP:** token authorization, input validation, status code, JSON shape, error text.
- **Browser:** loading, success, error, empty, disabled, focus, Escape, touch targets, overflow, reduced motion.
- **Extension:** plugin timeout and output limits, theme preservation, failure isolation.
- **Tests:** direct module coverage, integration coverage, standalone execution, no assumptions about the user's data directory.

## Known tradeoffs and future seams

- JSON keeps the library portable and inspectable, but a very large catalog may eventually need an indexed read model.
- The browser UI is fast to iterate and full-featured, but `index.html` is a large inline surface and needs disciplined state boundaries.
- The native Tk shell keeps a low-dependency fallback, but feature parity requires duplicate presentation work.
- The web server is a strong local boundary, but sharing its token is equivalent to sharing control of the instance.
- Plugins are isolated enough to keep failures bounded, but they remain trusted local Python.
- Themes are safe to keep presentation-only, but cannot express new workflows.
- `web_app.py` is the orchestration boundary. Further extraction could separate API routing, process lifecycle, and feature modules without changing the browser contract.

The stable seams for future work are the normalized game identity, `JsonStateStore`, tokenized launch contract, session event stream, plugin hook payloads, and theme CSS boundary.
