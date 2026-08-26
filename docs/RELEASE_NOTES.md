# OpenBox v1.7.0

OpenBox v1.7.0 is the guided-setup and durable-operations release. A first-time user can go from an empty library to imported, enriched, launch-ready games without browser `prompt()`, hidden cross-menu steps, or silent mutations.

## Highlights

### Library Setup Center

- Guided **Set up library** workflow with an eight-step stepper: welcome, sources, scan preview, import review, emulator readiness, metadata match review, launch preflight, and completion.
- Side-effect-free scan previews with idempotent commit, stale-preview guards, and `import_batch_id` tagging for post-import filtering.
- Every mutating action previews before commit; Setup never POSTs `/api/launch` directly.

### Activity Center

- Durable operation service backed by `operations.json` with queued, running, cancelling, done, partial, error, cancelled, and interrupted states.
- Persistent top-bar Activity control with SSE progress, cancellation, retry/resume, and legacy `/api/jobs` compatibility.

### Launch Doctor

- Preflight validation for game paths, adapters, Flatpak/native executables, BIOS/firmware, and tokenized launch arguments.
- Registry-driven emulator detection with explicit ambiguity handling and launch precedence rules (per-game launch → adapter → profile → detected adapter → direct exe).

### Additive v2 API

- Exact `/api/v2/*` routes for setup preview/commit, emulator registry, launch preflight, metadata match review, and durable jobs (see ADR 0010).
- Stable error codes for preview staleness, unresolved candidates, job conflicts, and cloud sync failures.
- Canonical `library.json` schema remains version **6**; previews and operations use separate disposable storage.

### Performance and scale

- Formal support target of **20,000** games with query-cache correctness, bounded index fallback, and performance gates.

### Packaging

- Release-gated **x86_64** artifacts: Ubuntu 22.04 **AppImage** and **Flatpak** (runtime 25.08).
- No Flathub store submission in this release; no telemetry added.

## First-time empty → launch-ready

1. Open OpenBox with an empty library; the empty state offers **Set up library** (or use **Tools → Setup** / top-bar **Setup**).
2. Walk the Setup Center stepper: choose import sources, preview scans, review paginated candidates, install or verify emulators, run metadata match review, and batch preflight with Launch Doctor.
3. Commit imports and enrichment from preview; Activity shows long-running work with SSE progress and recovery.
4. Launch from the library or Big Box; Launch Doctor explains readiness before execution.

## Explicit exclusions (v1.7)

- No SQLite / 50k library model (formal scale is 20,000 games).
- No ARM64 artifacts (x86_64 Linux only).
- No i18n/localization (English-only UI).
- No Flathub store submission (bundle is release-gated only).
- No schema 7 bump; previews and operations stay in sidecar files.
- No telemetry; diagnostics never upload.
- No v1 route breakage or removal of legacy parity shims.

## RC soak checklist (48–72 hours)

Before tagging a **stable** v1.7.0 release, maintainers run a prerelease soak on exact CI artifacts:

1. Tag a prerelease (e.g. `v1.7.0-rc.1`) and publish AppImage + Flatpak bundle from the exact-artifact CI job.
2. Soak **48–72 hours** on x86_64 hardware (Ubuntu LTS, Fedora, Arch, or Steam Deck).
3. Exercise Setup Center end-to-end, Activity SSE/cancel/retry, Launch Doctor preflight, metadata match review, and Big Box launch.
4. Confirm `make check` green on the release tag; `python3 -B scripts/check_version_sync.py` exit 0.
5. Run `python3 -B tests/test_packaging.py` and `./scripts/ui_smoke.sh` against the artifact.
6. Verify no telemetry in artifacts, Flatpak `finish-args`, or `/api/diagnostic` output.
7. After soak passes, publish stable `v1.7.0` with signed artifacts and SBOM per ADR 0013.

## Screenshots

Regenerate README screenshots for Setup Center and Activity with:

```bash
cd scripts && npm ci
python3 scripts/capture_readme_screenshots.py
```

Requires Node.js 22.12+ and a display (or use `scripts/capture_screenshot_puppeteer.mjs` headless where supported).

The full changelog is available at https://github.com/vindeckyy/OpenBoxGL/compare/v1.6.0...v1.7.0.
