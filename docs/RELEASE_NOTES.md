# OpenBox v1.7.1: Play Insights & Performance Polish

**OpenBox v1.7.1** focuses strictly on performance optimizations, the brand-new local Play Insights dashboard, and actionable polish for Setup and Launch Doctor.

---

### 📊 Local-First Play Insights
* **366-Day Activity Heatmap**: Visualizes your daily play sessions across 5 intensity levels (0–4).
* **Streaks & 30-Day Momentum**: Tracks your current streak, longest streak, and compares playtime over the last 30 days against the prior 30.
* **Top Platforms & Genres**: Instant breakdown of your most-played systems and genres.
* **Instant & Private**: Computes stats on-the-fly from existing `history` and `games` data in under **15 ms** for 20,000 entries with zero telemetry and no database migrations.
* **Accessible & Lazy-Loaded**: Loaded on demand (`static/insights.js`) with an accessible HTML table fallback for screen readers. Backed by `GET /api/v2/insights/summary` and `GET /api/v2/insights/heatmap`.

---

### ⚡ 20k Performance Engine
* **Virtual Spacer-Window Grid**: Library grid now renders through a virtualized `IntersectionObserver` window with `contain-intrinsic-size`, dramatically reducing DOM node overhead during fast scrolling while preserving card focus and context menus. Includes a `localStorage['openbox-virtual-grid']` fallback switch.
* **Off-Thread Trigram Search**: Search indexing and query evaluation run in a background Web Worker (`static/worker.search.js`) to eliminate typing stutter, with automatic fallback to the main thread.
* **Bounded Facet LRU Cache**: `pkg/state/cache.py` adds a 64-entry `FacetCache` with execution time budgeting and cache-epoch bumps on invalidation.
* **Micro-Batch Write Coalescing**: `state_store.py` coalesces disk writes within 50 ms windows into a single `fsync` to eliminate disk churn during rapid state mutations.
* **CI Benchmark Suite**: Added `--json-out` support to `scripts/perf_bench.py` to continuously enforce <15 ms query gates at 20,000 games.

---

### 🩺 Actionable Launch Doctor Fixes
* **Interactive Fix Actions**: Blocking preflight checks now attach actionable `fix_action` triggers (`{kind, label, payload}`) to render direct buttons instead of static error badges:
  * `flatpak_install`: One-click button to install missing emulator Flatpaks.
  * `reveal_bios_path`: Direct assistance showing the exact destination folder for missing BIOS/firmware files.
  * `pick_core`: Instant selector to pick emulator cores and launch adapters.
  * `explain_token`: Contextual explanations for misconfigured launch tokens.
* **Platform Disambiguation**: Renders platform chips and registry health status (`GET /api/v2/emulators/registry?health=1`) to resolve ambiguous launcher targets.

---

### 🧙 Setup Center Progress Polish
* **Human Progress Storytelling**: Scan previews now generate contextual status messages in `preview_document` (e.g. *"Found 342 games — 12 need your pick →"*) to clearly communicate scan state during candidate review.

---

### 🎨 Design System Tokens
* **9 New Tokens in `static/app.css`**: Added `--overlay-insight-cell-0` through `--overlay-insight-cell-4`, `--border-insight`, `--shadow-insight`, `--surface-insight-card`, and `--focus-ring`.
* **5 Stock Themes Updated**: Full token coverage with zero raw hex values across all themes.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.0...v1.7.1


