### Fixed

- Resolved frontend module reference errors, prevented media dialog DOM accumulation, fixed play queue and tag selected game lookups, and expanded BigBox menu controller navigation.
- Worker job queues and webapp state cleanup now guarantee cleanup on failure and process termination to prevent queue and process leaks.
- Preserved shared media files during media deletion via realpath reference counting.
- Stale Unix socket single-instance focus handling and native host boot cleanup now recover cleanly from crashed instances.

### Changed

- Added Proton and Wine prefix manager, Faugus scan and import, Eden Switch emulator definition, and canonical identity deduplication.
- Added LaunchBox and Playnite SDK parity features: Edit Game modal navigation, acronym title search, capability filter rules, scoped media cleanup, play stats reset, and dynamic launch variables.

### Hardened

- Active sessions are now persisted and reconciled against PID, start time, and cmdline verification, marking abandoned sessions and reattaching watchers.
- Launch preparation is transactional with explicit 8-phase lease tracking.
- Backups and exports centralize credential and secret redaction with manifest tracking, preserving local secrets on restore.
- Performance benchmark runner covers full operational matrix with 10k game scaling gates.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.4.0...v1.5.0.
