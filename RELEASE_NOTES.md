### Fixed

- SteamOS AppImages no longer export bundled libraries into the host shell, fixing startup failures where `/bin/bash` could not resolve `rl_print_keybinding`.
- Webhook delivery rejects non-public destinations, pins validated DNS results, disables proxies and redirects, and bounds response reads.
- Library and save backups use private atomic files, reject unsafe archive entries, and protect restore paths against symlinks and archive replacement races.
- The native bridge now accepts messages only from the exact OpenBox scheme, host, and port.
- Gameyfin validates IDs before path construction, keeps installation and removal inside the configured root, requires HTTPS, and verifies supplied checksums.
- 7z extraction rejects link entries, snapshots the archive before validation, and validates the staged tree before promotion.
- Media and document endpoints enforce approved roots, including symlinked parent checks.
- Environment loading no longer searches the current directory and accepts only owner-controlled files and supported keys.
- Job bookkeeping, executor queues, SSE subscribers, and per-client event queues now have explicit limits and cleanup behavior.

### Changed

- Release publication now requires an Ed25519 signature and a pinned public key. Missing signing material fails the release instead of publishing checksum-only artifacts.
- The release workflow separates build, provenance attestation, and publication permissions, validates the tag against the application version, and refuses to overwrite existing assets.
- AppImageTool, Python development tools, browser tooling, and CI dependencies are pinned; the generated SBOM inventories the completed AppImage.
- Remote plugin catalogs require a pinned catalog digest, HTTPS package URLs, and package checksums.
- Puppeteer was upgraded to 25.7.0; `npm audit` reports zero vulnerabilities.

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.1.0...v1.2.0.
