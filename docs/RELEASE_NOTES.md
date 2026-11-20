# OpenBox 3.0.0: The Rust-y Box

**OpenBox 3.0 ("The Rust-y Box") replaces the Python backend with a standalone native Rust server (`openbox-server`), delivers dramatic speed and memory upgrades across every library size up to 50,000 games, polishes the desktop UI from top to bottom, and adds five new player features without changing `library.json` schema 7.**

Your existing schema 7 library opens unchanged with zero migration. A 2.1 build can still open a 3.0 library, every `/api/v2` and `/api/host` contract stays intact, and release packages for Linux, Windows, and macOS now ship the native Rust binary directly with no bundled Python runtime.

The complete list of changes is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), the code diff is in [v2.1.0...v3.0.0](https://github.com/vindeckyy/OpenBoxGL/compare/v2.1.0...v3.0.0), and upgrade steps for older libraries are in [upgrade-3.0.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/upgrade-3.0.md).

## The Rust Migration and Performance Upgrades

All 417 HTTP endpoints (413 public `/api/v2/*` routes plus 4 `/api/host/*` native-window routes) and all core storage, search, emulator launch, metadata, sync, and plugin host subsystems now run natively in Rust (`openbox-server`, ADR 0115).

- **50,000-game scale graduation (ADR 0116):** The previous Python backend missed the 1,500 MB memory budget at 50,000 games (2,163.3 MB RSS). On Rust, a 50,000-game library finishes the full benchmark at **468.8 MB RSS** (31.3% of the budget), graduating OpenBox's formal supported scale from 20,000 to **50,000 games** while keeping `library.json` as the single human-readable canonical store.
- **Nearly 3x less memory:** At 50,000 games, Rust uses **468.8 MB** vs Python's **1,281.9 MB** on the same machine (**2.73x lower RSS**). At 10,000 games it uses **113.4 MB**, and at 20,000 games it uses **204.1 MB**.
- **Faster on every single server endpoint:** Streaming JSON and gzip projections, zero-copy cached payloads, in-place warm transactions, O(k) delta search, and a zero-allocation media probe cache beat Python across 10k, 20k, and 50k libraries on both median and p95 latency.
- **No bundled Python runtime:** Linux AppImages, the Linux Flatpak, Windows portable zips, and the universal macOS `.app` bundle now run directly on `openbox-server`. Startup is faster and bundles carry zero embedded CPython runtime (host Python 3.11+ is only invoked on demand if you run `.py` v2 plugins).

### 50,000-Game Head-to-Head Benchmark (Same Machine, Python 3.0 vs Rust 3.0)

Measured on Linux x86_64 (AMD Ryzen 5 4600H, 16 GiB RAM, `scripts/perf_bench.py --sizes 10000,20000,50000 --runs 3 --memory`):

| Metric (p95) | Python 3.0 (`web_app.py`) | Rust 3.0 (`openbox-server`) | Upgrade |
|---|---:|---:|---|
| Server RSS after benchmark | 1,281.9 MB | **468.8 MB** | **2.73x less RAM** |
| Favorite write mutation | 2,950.6 ms | **560.4 ms** | **5.27x faster** |
| Bulk write mutation | 2,903.3 ms | **345.9 ms** | **8.39x faster** |
| Cover media serving (`/api/v2/media`) | 2,346.9 ms | **0.4 ms** | **5,867x faster** |
| Explorer facets (`/api/v2/explorer/facets`) | 2,998.0 ms | **21.1 ms** | **142.1x faster** |
| Delta search (`/api/v2/library/delta`) | 41.3 ms | **0.5 ms** | **82.6x faster** |
| Insights milestones (`/api/v2/insights/milestones`) | 2,599.9 ms | **157.5 ms** | **16.5x faster** |
| Smart picker (`POST /api/v2/library/pick`) | 544.1 ms | **64.8 ms** | **8.40x faster** |
| Paginated library (`offset=0&limit=500`) | 12.4 ms | **1.5 ms** | **8.27x faster** |
| Constellation graph (`limit=400`) | 266.1 ms | **42.1 ms** | **6.32x faster** |
| Full library transfer (gzip) | 5.9 ms | **4.8 ms** | **1.23x faster** |

### 10,000- and 20,000-Game Highlights (`openbox-server`)

| Library Size | Write p95 | Favorite p95 | Bulk p95 | Library gzip p95 | Library page p95 | Picker p95 | Constellation p95 | End RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **10,000 games** | 110.6 ms | 110.0 ms | 70.2 ms | 1.3 ms | 1.3 ms | 14.6 ms | 13.8 ms | **113.4 MB** |
| **20,000 games** | 220.7 ms | 216.1 ms | 144.6 ms | 2.4 ms | 1.3 ms | 27.1 ms | 19.6 ms | **204.1 MB** |

## UI and Usability Improvements

Alongside the Rust migration, 3.0 includes a focused usability pass across the main library, sidebar, details pane, dialogs, and Big Box mode:

- **More games above the fold:** Compacted the sticky library header into a single row (`94 px` down from `226 px`) by moving Artwork and Layout into a `Display` popover, auto-hid the drag-and-drop banner until files are actively dragged over the window, made `Jump back in` a slim dismissible strip, hid the redundant `PORTRAIT` group banner when all covers share one shape, and auto-collapsed the empty right Details pane until a game or collection is selected. On a `1280x720` laptop screen, 6 full game cards now sit completely above the fold (up from 0).
- **Direct Settings and a searchable Tools menu:** Promoted **Settings** directly into the top bar, added a live `Filter tools...` search box with keyboard navigation at the top of the **Tools** menu, reorganized Tools into a 3-column layout so all 41 items fit inside a `1440x900` viewport without scrolling, and clarified tool labels and hints across all 5 locales.
- **Focused Game Details pane:** Added a visible close (`x`) button and `Escape` shortcut to dismiss Details, kept the 4 primary actions upfront while moving secondary tools into a collapsible **More actions** section, removed the duplicate `Moments` button, hid blank `-` fields in `Information`, deduplicated read-only `Backlog` stats, and made **Save management** and **Launch check** collapsible cards (with Launch Check auto-expanding whenever an issue needs attention).
- **Cleaner sidebar hierarchy:** Ordered the sidebar into `Search`, `View`, `Platforms`, `Categories`, `Playlists`, and a collapsed `More filters` section (tucking `ESRB`, `Source`, `Exclude`, and `Explorer` out of the way), and hid empty `Filter presets` and `Collections` walls until populated.
- **Polished dialogs, list view, activity, and Big Box:**
  - **Guided Setup:** Fixed sequential `1` through `6` step numbering, removed duplicated helper text on the source picker, and disabled `Continue` until at least one source is selected.
  - **Edit Game:** Reorganized `Basics` into a compact 2-column layout with `Name` first and paired short fields.
  - **Filters Drawer:** Switched smart toggles to a 2-column grid and pinned `Apply smart filters` in a sticky footer so it never clips.
  - **Surprise Me Picker:** Picks a recommendation immediately on open and cycles suggestions when clicking `Another`.
  - **Settings:** Pinned the header, category nav, sticky `Search settings` bar, and action footer; isolated `Remove all imported Steam games` inside a clear bottom danger zone.
  - **Cards and List View:** Moved the hover star-rating bar to the top of card covers so it never overlaps badges, hid the grid-only `Display` menu in List view, and removed duplicated progress/rating pills from List view titles.
  - **Activity Drawer:** Switched routine running job badges from red to a calm neutral pill (reserving red strictly for errors needing attention), compacted filters into a 2-column row, humanized internal job types, and removed redundant single-job `Grouped by run` duplicates.
  - **Big Box:** Added a clickable `Exit` button in the top bar and fixed portrait cover image framing in Stage mode to eliminate the dark bottom chin.

## Five New Player Features in 3.0

- **Save Vault (`save-vault/`, ADR 0109):** Automatic or manual SHA-256 save snapshots from the tabbed Save History dialog, on game close (`save_vault_on_close`), or on a schedule (`save_vault_auto_days`), with mandatory pre-restore safety snapshots and automatic exclusion of shared emulator roots.
- **Achievement Trail (ADR 0110):** Offline RetroAchievements analytics over `cache/retroachievements/` with 8-week unlock velocity charts, confidence-rated ETAs, 100-point milestones, and cheapest-first next-up queues.
- **Deal Radar (ADR 0111):** Opt-in (`deals_enabled=false` by default) Steam watchlist and manual price tracker restricted to `https://store.steampowered.com` with durable drop-notification tracking.
- **Player's Log: Time Capsules and Milestone Cards (ADR 0112):** Sealed notes and questions that unlock on a chosen local date, plus 9 deterministic milestone kinds with offline 1200x630 PNG card export.
- **Backlog Itinerary (ADR 0113):** Multi-week backlog planner with `quick-wins`, `comeback`, and `raided` strategies, IGDB and play-history hour estimates, RetroAchievements progress refinement, and live status tracking.

## Contract Changes in 3.0

1. **Opening a library no longer migrates it:** Schema 7 opens unchanged. Schema 6 waits for `openbox --migrate-library` (writing `library.v6.backup.json` first). Schema 1 through 5 requires opening once on OpenBox 2.1 before upgrading to 3.0.
2. **Rollback and plugin API v1 are removed:** `openbox --rollback-library` exits 4 without touching files (use 2.1.0 to restore a v6 backup). Plugins must declare `"api_version": 2`.
3. **Lifecycles declared in one table (ADR 0107):** Background jobs, metadata sync, match preview, and launch audit share one unified state machine table.
4. **Python 3.11+ floor for source runs and Python plugins:** Prebuilt 3.0 binaries run natively on Rust `openbox-server` without Python. Running from Python source (`OPENBOX_BACKEND=python`) or executing `.py` v2 plugins requires Python 3.11 or newer.

| Contract | 2.1.0 | 3.0.0 |
|---|---|---|
| Server backend | Python (`web_app.py`) | Native Rust (`openbox-server`) |
| Formal scale | 20,000 games | **50,000 games** |
| Public API | `/api/v2` | `/api/v2` |
| Native host channel | `/api/host` | `/api/host` |
| Library schema | 7 | 7 (no migration on open) |
| Backup format | Write 2, read 1 and 2 | Write 2, read 1 and 2 |
| Plugin API | 2, with v1 adapter | 2 only |
| Sync format | 3 | 3 |
| Canonical store | `library.json` | `library.json` |

Deleting `sqlite_readmodel.db` still loses nothing. The projection rebuilds automatically from `library.json`.

## Get OpenBox 3.0.0

| Platform | Download | Notes |
|---|---|---|
| Linux x86_64 | `OpenBox-x86_64.AppImage` | Native Rust binary; glibc 2.35 floor; zsync available |
| Linux aarch64 | `OpenBox-aarch64.AppImage` | Native Rust binary; glibc 2.35 floor; zsync available |
| Linux Flatpak | `OpenBox-x86_64.flatpak` | Native Rust binary; GNOME 51 runtime |
| Windows x86_64 | `OpenBox-x86_64-windows.zip` | Includes native WebView2 host and `openbox-server.exe` |
| Windows ARM64 | `OpenBox-arm64-windows.zip` | Includes ARM64 WebView2 host and `openbox-server.exe` |
| macOS | `OpenBox-macos.zip` | Universal (`arm64` + `x86_64`) native host and `openbox-server`; ad-hoc signed |

On macOS, install with the signed installer:

```bash
VERSION=3.0.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

On Windows, extract the release archive and run `OpenBox\scripts\install.ps1`. On Linux, use the AppImage, the Flatpak, or `scripts/install.sh`.

## Known Limits

- The macOS bundle is ad-hoc signed, so first launch and updates require Privacy & Security confirmation.
- Linux-only features (such as Steam Game Mode / Gamescope and Flatpak management) remain hidden on Windows and macOS by design.
- OpenBox 1.19 cannot read a schema 7 library.
- Flathub submission is tracked separately.
