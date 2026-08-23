### Architecture & State Management

- Decomposed `webapp_state.py` into focused state modules: `pkg/state/imports.py` (import orchestration, duplicate merging, auto-import), `pkg/state/commands.py` (command execution), and `pkg/state/registry.py` (process and session tracking with typed `Session` dataclass), keeping a lightweight backwards-compatible re-export facade.
- Centralized launch token expansions (`{path}`, `{name}`, `{dir}`, `{stem}`, `{platform}`, `{app_id}`, `{heroic_app_id}`, `{lutris_id}`, `{rom_name}`, `{DataDir}`, etc.) into `pkg/parity/launch_tokens.py`.
- Consolidated caches and locks into coordinated `CacheEpoch` dataclass with atomic full invalidation (`_invalidate_all()`).

### Frontend & Accessibility

- Replaced details/summary tools dropdown with fully accessible WAI-ARIA button and menu pattern (`#toolsButton`, `#toolMenu`, and `#toolsWrap.open`) supporting full keyboard navigation (Arrows, Home, End, Escape, Tab).
- Memoized search index with LRU cache, debounced input, and bounded trigram expansion for instant lookups across 20k+ games.
- Implemented dialog focus traps with inert fallbacks and proper focus restoration.
- Memoized grid geometry calculation for faster library view rendering.

### Security & Hardening

- Added `frame-ancestors` directive to Content-Security-Policy (CSP) headers.
- Hardened exception handling across handlers, state imports, commands, and SSE streams to eliminate broad except catches and add structured error logging.
- Added input validation and authentication checks to native dialog, window, and emulator scan endpoints.
- Added performance benchmark write-path gate (<500ms for 10k games).

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.5.1...v1.6.0.

