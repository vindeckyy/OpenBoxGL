### Performance

- Optimized state writes with dirty-field tracking, batched snapshot persistence, and cached library projections for large libraries.
- Accelerated import scanning, metadata batching, and archive inspection throughput.
- Streamlined BigBox CoverFlow rendering and indexed title search matching.
- Improved native host startup responsiveness with non-blocking IPC polling.

### Changed

- Enhanced cross-store import consolidation across Steam, Heroic, Lutris, Faugus, and ROMs using canonical identity normalization.
- Hardened CLI help formatting and argument parsing for headless and native host invocations.
- Updated parity compatibility shims and LaunchBox feature matrix documentation.

### Fixed

- Ensured completed background job futures are released synchronously to eliminate future and memory retention.
- Hardened import endpoint error handling and input validation against malformed payload structures.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.5.0...v1.5.1.

