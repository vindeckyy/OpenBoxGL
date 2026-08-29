# OpenBox v1.7.1: Polish, Performance & Play Insights

**OpenBox v1.7.1** is a refinement and performance release built on top of 1.7.0. It delivers local-first gaming analytics, fluid responsiveness at 20,000-game scale, actionable one-click launch fixes, and human-friendly setup storytelling—all while upholding OpenBox's core design tenets: **100% offline, zero accounts, zero telemetry, and zero runtime dependencies**.

---

## 🌟 Highlights

### 📊 Local-First Play Insights Dashboard
Gain rich visual insights into your gaming patterns without sending a single byte over the network. The new Play Insights panel computes real-time statistics directly from your existing local play logs:

* **366-Day Activity Heatmap**: A GitHub-style activity grid across 5 intensity levels (0–4) visualizing your gaming cadence over the past year.
* **Streak & Momentum Tracking**: Monitor your current active streak, all-time longest streak, and 30-day playtime momentum (comparing hours played in the last 30 days against the previous 30 days).
* **Platform & Genre Breakdown**: Dynamic ranked breakdown of your most-played systems and favorite genres.
* **Lightning Fast & Lightweight**: Computes the full 366-day heatmap across 20,000 history entries in under **15 ms** with zero extra database storage.
* **Accessibility First**: Features lazy-loaded UI components (`static/insights.js`) and a semantic HTML table fallback for screen readers.
* **Additive REST Endpoints**: Backed by `GET /api/v2/insights/summary` and `GET /api/v2/insights/heatmap?days=&end_date=`.

---

### ⚡ 20,000-Game Performance Engine
OpenBox is built to remain snappy, smooth, and lightweight even with massive ROM collections:

* **Spacer-Window Virtual Grid**: The library grid now virtualizes rendering using an `IntersectionObserver` window with `contain-intrinsic-size`, drastically cutting DOM overhead for 20k+ libraries while preserving smooth rAF scrolling, context menus, and active keyboard/card focus. *(Includes a `localStorage['openbox-virtual-grid']` kill-switch fallback).*
* **Off-Thread Worker Search**: Trigram title search indexing and querying now execute asynchronously off the main UI thread via `static/worker.search.js`, ensuring zero input lag when typing. Automatically falls back to the main thread if Web Workers are unavailable.
* **LRU Facet Caching**: `FacetCache` now features a 64-entry LRU cache with strict time-budget guards and atomic cache epoch invalidation (`pkg/state/cache.py`).
* **Micro-Batch Persistence**: State updates coalesce across 50 ms micro-batches with single-`fsync` persistence in `state_store.py`, eliminating disk thrashing during rapid operations.

---

### 🩺 Actionable Launch Doctor
Launch Doctor transitions from passive preflight inspection to active, one-click troubleshooting for missing emulators and misconfigurations:

* **Direct Fix Actions**: Every blocking preflight check now provides an interactive `fix_action` button directly in the detail pane:
  * `flatpak_install`: Install missing emulator Flatpaks with a single click.
  * `reveal_bios_path`: Immediately reveal the target folder for missing BIOS and firmware files.
  * `pick_core`: Seamlessly select and assign compatible emulator cores or launch adapters.
  * `explain_token`: View contextual guidance and fixes for invalid launch token parameters.
* **Platform Chips & Disambiguation**: Clear platform badges and registry health endpoints (`GET /api/v2/emulators/registry?health=1`) eliminate guesswork before launching.

---

### 🧙 Setup Center & Storytelling Polish
* **Human-Centric Progress Messages**: Import scan previews now provide clear, human progress storytelling (e.g., *"Found 342 games — 12 need your pick →"*).
* **Safe, Idempotent Imports**: Previews remain completely side-effect free, feature stale-preview guards if files change mid-scan, and tag imports with `import_batch_id` for quick post-import filtering.

---

### 🎨 Design System & Theme Harmony
* **Comprehensive Token Contract**: Added 9 new design tokens to `static/app.css` `:root` (`--overlay-insight-cell-0` through `--overlay-insight-cell-4`, `--border-insight`, `--shadow-insight`, `--surface-insight-card`, `--focus-ring`).
* **All 5 Stock Themes Updated**: Flawless, token-accurate styling across every stock theme with zero raw hex values outside `:root`.

---

## 🛡️ Non-Negotiables & Guarantees

* **Zero Telemetry**: No tracking, no analytics, no remote phone-home. Your data never leaves your machine.
* **Dependency-Free Runtime**: Pure Python 3.10+ standard library backend and vanilla JavaScript frontend.
* **Frozen v1 API Surface**: 100% backward compatibility preserved across all 60 v1 route endpoints.
* **Strict Quality Gates**: 76 test suites passing at 100% with enforced coverage and sub-15ms query benchmarks.

---

## 📦 Getting OpenBox v1.7.1

OpenBox v1.7.1 is distributed as standalone, release-gated Linux packages for x86_64:

* **AppImage**: Standalone, portable executable built on Ubuntu 22.04 LTS with integrated `zsync` delta updates and SBOM.
* **Flatpak**: Sandboxed Flatpak bundle built against the Freedesktop 25.08 runtime.

### Upgrade & Changelog

To upgrade your existing installation, download the latest package or pull the tagged commit:

```bash
git fetch --tags
git checkout v1.7.1
```

For the complete list of commits and file diffs, view the [Full v1.7.0...v1.7.1 Comparison](https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.0...v1.7.1).

