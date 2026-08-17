### Hardened

- Plugin execution now uses bubblewrap with isolated namespaces, no network access, and temporary mounts for home, temporary, runtime, and removable-media paths when the host supports it. If the sandbox cannot be created, enabled plugins are skipped by default; `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` is an explicit opt-in for trusted local plugins.
- Gamescope regression tests now launch in their own process groups and terminate the full group on timeout, preventing nested gamescope processes from surviving the test gate.

### Changed

- Cloud sync, save and backup restore, launch handling, settings validation, metadata application, Lutris import, 7z validation, webhook validation, filter matching, and game resolution were split into focused helpers, keeping the security-sensitive paths easier to audit and test.
- The background job manager now has dedicated coverage for retries, cancellation, queue and name limits, bounded results, shutdown, and completed-future cleanup.
- Dead backend code and obsolete browser, screenshot, migration, and performance-capture scripts were removed.
- From-source setup documentation now explains the tokenized UI URL and the supported `OPENBOX_ENV_FILE`, data-directory, home-directory, and user-config locations.
- CI and release tooling now use the refreshed GitHub Actions and JavaScript dependencies, with CodeQL action components kept on one version and grouped Dependabot updates for future changes.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.2.0...v1.3.0.
