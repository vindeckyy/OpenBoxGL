### Fixed

- AppImage and Flatpak now bundle every static module, fixing 404s for util.js, state.js, library.js and other chunks that left the top bar unresponsive. Routes now handle future JavaScript chunks without manual table updates. Fixes #19.
- Build provenance, SBOM generation and packaging tests now cover the full static set so the UI ships complete.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.3.0...v1.4.0.
