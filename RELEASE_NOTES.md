### Fixed

- AppImage and Flatpak now bundle every static module, fixing 404s for util.js, state.js, library.js and other chunks that left the top bar unresponsive. Routes now handle future JavaScript chunks without manual table updates. Fixes #19.
- AppImage bundling now creates parent directories for pkg/parity modules so the build does not fail on packaged layouts.
- Build provenance, SBOM generation and packaging tests now cover the full static set so the UI ships complete.

### Changed

- Repository layout reorganized: docs moved to docs/, tests to tests/ and parity modules to pkg/parity with backwards compatible shims at root. Agent conventions and layout docs added.
- CI, test runner, coverage gates and token hygiene updated to handle both flat and packaged layouts. Docs paths fixed after reorg.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.3.0...v1.4.0.
