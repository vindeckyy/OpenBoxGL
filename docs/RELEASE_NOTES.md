### Changed

- Added Proton and Wine prefix manager (`/api/wine/*`), Faugus launcher scan and import (`/api/faugus/*`), and Eden Switch emulator definition (`emulator_defs/eden.yaml`).
- Added canonical identity normalization across Steam, Heroic, Lutris, Faugus, and ROMs with duplicate game consolidation in library health checks.
- Added LaunchBox and Playnite SDK parity features: Edit Game modal previous/next navigation, acronym search title matching (e.g. `oot`, `mgs`, `sotn`), capability filter rules (`has_saves`, `has_achievements`, `has_missing_media`, `has_highscores`), scoped per-platform media cleanup, and play statistics reset in context menus and bulk edit.
- Expanded dynamic launch variables for emulator and custom commands (`{ImagePath}`, `{dir}`, `{Dir}`, `{file}`, `{File}`, `{stem}`, `{FileNameWithoutExtension}`, `{Platform}`, `{EmulatorDir}`, `{DataDir}`).
- Added `--fullscreen-width`, `--fullscreen-height`, and `--resolution <WxH>` CLI flags for kiosk and native window sizing, plus `Ctrl+Alt+Q` / `Ctrl+Alt+R` shuffle shortcuts.

### Fixed

- Resolved frontend module reference errors, prevented media dialog DOM accumulation, fixed play queue and tag selected game lookups, and expanded BigBox menu controller navigation.
- Worker job queues and webapp state cleanup now guarantee cleanup in try/finally blocks to prevent queue, future, and process leaks.
- Preserved shared media files during media deletion via realpath reference counting, returning accurate deleted and shared lists.
- Stale Unix socket single-instance focus handling and native host boot cleanup now recover cleanly from crashed instances.
- Fixed BigBox mode exit response hang, related game query lookups, and emulator app_id validation.

### Hardened

- Active sessions are now persisted and reconciled on startup against PID, start time, and cmdline verification, marking abandoned sessions and reattaching watchers.
- Launch preparation is transactional with explicit 8-phase lease tracking.
- Backups and exports centralize credential and secret redaction with manifest tracking, preserving local secrets on restore.
- Performance benchmark runner covers full operational matrix with 10k game scaling gates.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.4.0...v1.5.0.
