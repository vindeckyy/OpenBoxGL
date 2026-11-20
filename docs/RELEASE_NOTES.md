# OpenBox 3.0.0: The Rust-y Box

**Don't let the name fool you: there is nothing rusty about OpenBox 3.0. We replaced the entire Python backend with a standalone native Rust server (`openbox-server`), redesigned the desktop workspace so your games come first, and packed in five new player features, all while keeping your existing 2.x library 100% compatible.**

This is the biggest leap in OpenBox history. Every cover load, live search, filter toggle, bulk edit, and library launch now runs on compiled native code instead of an embedded Python runtime. If you have been running OpenBox 2.x, updating takes less than a minute and you will feel the difference on the very first click.

> [!TIP]
> **Zero-friction upgrade for OpenBox 2.0 and 2.1 users:** Install 3.0 over your existing copy and open it. Your `library.json` (schema 7), box art, saves, settings, themes, and playlists load untouched with **zero migration and zero re-scraping**. OpenBox 2.1 can even still open a 3.0 library, so trying 3.0 is completely reversible.

Full changelog: [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) | Code diff: [v2.1.0...v3.0.0](https://github.com/vindeckyy/OpenBoxGL/compare/v2.1.0...v3.0.0) | Coming from 1.x? Read [upgrade-3.0.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/upgrade-3.0.md)

## At a Glance: What You Will Feel Immediately

- **Instant covers, search, and filtering:** At 50,000 games, cover serving is **5,867x faster** (`0.4 ms` vs `2,346.9 ms` p95), Explorer facets are **142.1x faster** (`21.1 ms` vs `2,998.0 ms`), and live delta search drops to **0.5 ms** (**82.6x faster**).
- **Nearly 3x less memory:** At 50,000 games, server RAM drops from **1,281.9 MB** to **468.8 MB** (**2.73x less RAM**). At 10,000 games, OpenBox sits at **113.4 MB**; at 20,000 games, **204.1 MB**. Leave RAM for your games and emulators, not your launcher.
- **Graduated to 50,000 games:** Formal supported scale more than doubles from 20,000 to **50,000 games** while keeping `library.json` human-readable and canonical.
- **No bundled Python runtime:** macOS downloads drop by **59.1%** (**17.4 MiB** down from **42.5 MiB**), Linux AppImages slim down to **9.7 MiB** (x86_64) and **9.0 MiB** (aarch64), and Windows portable zips now ship standalone `openbox-server.exe` so you can unzip and launch with no Python installed.
- **Your games back above the fold:** A sleek **94 px** single-row header (down from **226 px**) and auto-collapsing empty Details pane put **6 full game cards** cleanly above the fold on a `1280x720` laptop screen, up from **0**.
- **Five new player features built in:** **Save Vault**, **Achievement Trail**, **Deal Radar**, **Player's Log** (Time Capsules and Milestone Cards), and **Backlog Itinerary**.

## Native Rust Performance: Faster Everywhere You Click

All **417 HTTP endpoints** (413 public `/api/v2/*` routes plus 4 `/api/host/*` native-window routes) and all **170** backend modules now run natively in Rust (`openbox-server`). In day-to-day use, that translates directly to how your library feels:

- **Zero-stutter scrolling and instant search:** Streaming JSON and gzip projections, zero-copy cached payloads, O(k) delta search, and a zero-allocation media probe cache keep cover art, search-as-you-type, and Explorer browsing snappy even in massive multi-platform collections.
- **Snappy edits and Smart Picker rolls:** In-place warm transactions make favoriting a game **5.27x faster**, bulk edits **8.39x faster**, and Smart Picker recommendations **8.40x faster** at 50,000 games.
- **Built for 50,000-game collections:** The earlier Python 50,000-game graduation run (on Apple M4) missed OpenBox's 1,500 MB memory budget at `2,163.3 MB` RSS, keeping formal scale capped at 20,000 games, and even the same-machine Linux Python 3.0 run below takes `1,281.9 MB`. On Rust, the full 50,000-game benchmark finishes at **468.8 MB RSS** (**31.3%** of the budget), graduating OpenBox's formal scale from 20,000 to **50,000 games** without sacrificing a single-file human-readable `library.json`.
- **Lean, self-contained native packages:** Linux AppImages, the Linux Flatpak, Windows portable zips, and the universal macOS `.app` bundle all launch directly on `openbox-server` with zero embedded CPython runtime (host Python 3.11+ is only invoked on demand if you run `.py` v2 plugins).

### 50,000-Game Head-to-Head Benchmark (Same Machine: Python 3.0 vs Rust 3.0)

Measured on Linux x86_64 (AMD Ryzen 5 4600H, 16 GiB RAM, `scripts/perf_bench.py --sizes 10000,20000,50000 --runs 3 --memory`):

| Metric (p95) | Python 3.0 (`web_app.py`) | Rust 3.0 (`openbox-server`) | Upgrade |
|---|---:|---:|---|
| Server RSS after benchmark | 1,281.9 MB | **468.8 MB** | **2.73x less RAM** |
| Cover media serving (`/api/v2/media`) | 2,346.9 ms | **0.4 ms** | **5,867x faster** |
| Explorer facets (`/api/v2/explorer/facets`) | 2,998.0 ms | **21.1 ms** | **142.1x faster** |
| Delta search (`/api/v2/library/delta`) | 41.3 ms | **0.5 ms** | **82.6x faster** |
| Insights milestones (`/api/v2/insights/milestones`) | 2,599.9 ms | **157.5 ms** | **16.5x faster** |
| Smart picker (`POST /api/v2/library/pick`) | 544.1 ms | **64.8 ms** | **8.40x faster** |
| Bulk write mutation | 2,903.3 ms | **345.9 ms** | **8.39x faster** |
| Paginated library (`offset=0&limit=500`) | 12.4 ms | **1.5 ms** | **8.27x faster** |
| Constellation graph (`limit=400`) | 266.1 ms | **42.1 ms** | **6.32x faster** |
| Favorite write mutation | 2,950.6 ms | **560.4 ms** | **5.27x faster** |
| Full library transfer (gzip) | 5.9 ms | **4.8 ms** | **1.23x faster** |

<details>
<summary><strong>See 10,000- and 20,000-game benchmark numbers (<code>openbox-server</code>)</strong></summary>

| Library Size | Write p95 | Favorite p95 | Bulk p95 | Library gzip p95 | Library page p95 | Picker p95 | Constellation p95 | End RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **10,000 games** | 110.6 ms | 110.0 ms | 70.2 ms | 1.3 ms | 1.3 ms | 14.6 ms | 13.8 ms | **113.4 MB** |
| **20,000 games** | 220.7 ms | 216.1 ms | 144.6 ms | 2.4 ms | 1.3 ms | 27.1 ms | 19.6 ms | **204.1 MB** |

</details>

## UI and Usability Redesign: Built Around Your Games

OpenBox 3.0 pairs the Rust engine with a top-to-bottom usability pass across the library grid, sidebar, Details pane, Tools menu, dialogs, and Big Box mode so your box art stays front and center:

- **More games, less chrome:** We compacted the sticky library header into a single **94 px** row (down from **226 px**, reclaiming 132 px of vertical space) by tucking Artwork and Layout into a clean `Display` popover. The drag-and-drop banner now stays out of sight until you actually drag files over the window, `Jump back in` is a slim dismissible strip, the redundant `PORTRAIT` group banner disappears when all covers share one shape, and the empty right Details pane auto-collapses until you select a game or collection. On a `1280x720` laptop screen, **6 full game cards** now sit completely above the fold (up from **0**).
- **One-click Settings and an instant searchable Tools menu:** **Settings** is promoted directly into the top bar. The **Tools** menu gains a live `Filter tools...` search box with full keyboard navigation and a 3-column layout that fits all **41 items** inside a `1440x900` viewport without scrolling, alongside clearer labels and hints across all **5 locales**.
- **Focused, uncluttered Game Details pane:** Dismiss Details anytime with the new close (`x`) button or `Escape`. Your 4 primary actions stay upfront while secondary tools fold neatly into a collapsible **More actions** section. We removed the duplicate `Moments` button, hid blank `-` rows in `Information`, deduplicated read-only `Backlog` stats, and turned **Save management** and **Launch check** into collapsible cards (with Launch Check auto-expanding whenever an issue actually needs your attention).
- **Cleaner sidebar hierarchy:** Reordered into `Search`, `View`, `Platforms`, `Categories`, `Playlists`, and a collapsed `More filters` section (tucking `ESRB`, `Source`, `Exclude`, and `Explorer` out of your way), while hiding empty `Filter presets` and `Collections` walls until you populate them.
- **Every view and dialog sharpened:**
  - **Cards and List View:** Moved the hover star-rating bar to the top of card covers so it never covers badges, hid the grid-only `Display` menu in List view, and removed duplicated progress and rating pills from List view titles.
  - **Activity Drawer:** Switched routine running job badges from alarming red to a calm neutral pill (reserving red strictly for errors needing attention), compacted filters into a 2-column row, humanized internal job types, and removed redundant single-job `Grouped by run` duplicates.
  - **Big Box:** Added a clickable `Exit` button right in the top bar and fixed portrait cover framing in Stage mode to eliminate the dark bottom chin.
  - **Settings and Filters Drawer:** Pinned the Settings header, category nav, sticky `Search settings` bar, and action footer while isolating `Remove all imported Steam games` inside a clear bottom danger zone. Switched Filters Drawer smart toggles to a 2-column grid with a sticky `Apply smart filters` footer so it never clips.
  - **Guided Setup, Edit Game, and Surprise Me:** Fixed sequential `1` through `6` step numbering in Guided Setup, removed duplicated helper text on the source picker, and gated `Continue` until at least one source is selected. Reorganized Edit Game `Basics` into a compact 2-column layout with `Name` first. Made **Surprise Me** pick a recommendation immediately on open and cycle suggestions whenever you click `Another`.

## Five New Features Built for Players

OpenBox 3.0 isn't just faster and cleaner. It ships five brand-new features designed around how you actually play, collect, and finish games:

- **Save Vault (`save-vault/`):** Automatic or manual SHA-256-verified save snapshots right from the tabbed Save History dialog, on game close (`save_vault_on_close`), or on a schedule (`save_vault_auto_days`). Every restore creates a mandatory pre-restore safety snapshot first (exempt from auto-pruning), and shared emulator roots are automatically excluded so your vault stays safe and lean.
- **Achievement Trail:** Offline RetroAchievements analytics powered by your local `cache/retroachievements/` data. Track 8-week unlock velocity charts, confidence-rated completion ETAs, your next 100-point milestone, and cheapest-first next-up achievement queues, with explicit cancellable refresh jobs whenever you want fresh unlock dates.
- **Deal Radar:** An opt-in (`deals_enabled=false` by default) Steam price watchlist and manual price tracker. When enabled, all outbound traffic and redirects are strictly restricted to `https://store.steampowered.com` (no accounts, no cookies, no telemetry), with weekly default polling and durable price-drop notifications that never nag twice for the same price point.
- **Player's Log (Time Capsules and Milestone Cards):** Write sealed notes or questions to your future self that stay hidden server-side until your chosen local unlock date, plus **9 deterministic milestone kinds** with offline **1200x630 PNG** share card export styled with your active OpenBox theme.
- **Backlog Itinerary:** Turn your unfinished library into an achievable multi-week plan with `quick-wins`, `comeback`, and `raided` strategies. Combines IGDB and your own play-history completion estimates, refines remaining time from RetroAchievements progress, and tracks live `done`, `on_track`, `behind`, and `stale` status automatically as you play.

## Upgrade in Under a Minute

Every release package ships the native Rust `openbox-server` binary directly. AppImage, Windows, and macOS downloads include SHA-256 checksums and Ed25519 signatures verified by the installers and in-app updater.

| Platform | Download | Size (v3.0.0) | What's Inside |
|---|---|---:|---|
| **Linux x86_64** | [`OpenBox-x86_64.AppImage`](https://github.com/vindeckyy/OpenBoxGL/releases/download/v3.0.0/OpenBox-x86_64.AppImage) | **9.7 MiB** (down from 10.2 MiB) | Native Rust binary, zero bundled Python, glibc 2.35 floor, zsync delta updates |
| **Linux aarch64** | [`OpenBox-aarch64.AppImage`](https://github.com/vindeckyy/OpenBoxGL/releases/download/v3.0.0/OpenBox-aarch64.AppImage) | **9.0 MiB** (down from 10.1 MiB) | Native Rust binary, zero bundled Python, glibc 2.35 floor, zsync delta updates |
| **Linux Flatpak** | [`OpenBox-x86_64.flatpak`](https://github.com/vindeckyy/OpenBoxGL/releases/download/v3.0.0/OpenBox-x86_64.flatpak) | **7.1 MiB** | Native Rust binary bundled directly; GNOME 51 runtime |
| **Windows x86_64** | [`OpenBox-x86_64-windows.zip`](https://github.com/vindeckyy/OpenBoxGL/releases/download/v3.0.0/OpenBox-x86_64-windows.zip) | **14.6 MiB** | Standalone `openbox-server.exe` + native WebView2 host (no Python needed to launch) |
| **Windows ARM64** | [`OpenBox-arm64-windows.zip`](https://github.com/vindeckyy/OpenBoxGL/releases/download/v3.0.0/OpenBox-arm64-windows.zip) | **13.8 MiB** | Standalone ARM64 `openbox-server.exe` + native ARM64 WebView2 host (no Python needed to launch) |
| **macOS (Universal)** | [`OpenBox-macos.zip`](https://github.com/vindeckyy/OpenBoxGL/releases/download/v3.0.0/OpenBox-macos.zip) | **17.4 MiB** (down from 42.5 MiB, **59.1% smaller**) | Universal `arm64` + `x86_64` `OpenBox.app` + `openbox-server`; ad-hoc signed |

### Linux

Use the signed AppImage installer (auto-detects `x86_64` or `aarch64`, verifies key pin, SHA-256, and Ed25519 signature, and installs to `~/.local/bin`):

```bash
VERSION=3.0.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install.sh"
less install.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install.sh
```

Or download `OpenBox-x86_64.AppImage` / `OpenBox-aarch64.AppImage` directly (`chmod +x` and run), or install the Flatpak bundle with `flatpak install --user OpenBox-x86_64.flatpak`.

### macOS

Install or upgrade the universal `OpenBox.app` bundle with the signed installer (add `--replace` to the last line if replacing an existing `OpenBox.app`):

```bash
VERSION=3.0.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

### Windows

Download and extract `OpenBox-x86_64-windows.zip` (or `OpenBox-arm64-windows.zip`):

- **Portable launch (no Python required):** Run `OpenBox\openbox.cmd` directly from the extracted folder.
- **Full verified install (Start Menu + `openbox://` links + PATH):** Run `powershell -ExecutionPolicy Bypass -File .\OpenBox\scripts\install.ps1 -Tag "v3.0.0"` from PowerShell (verifies the Ed25519 signature using Python 3.11+ and installs to `%LOCALAPPDATA%\OpenBox`).

## Compatibility and Contract Notes

> [!NOTE]
> **Upgrading from OpenBox 2.0 or 2.1?** Nothing breaks. Schema 7 opens unchanged, every `/api/v2` and `/api/host` contract stays identical, and deleting `sqlite_readmodel.db` still loses nothing (the search projection rebuilds automatically from `library.json`).

- **Coming from 1.19 (schema 6) or earlier:** Opening a library no longer migrates it automatically. Schema 6 waits for `openbox --migrate-library` (writing `library.v6.backup.json` first). Schema 1 through 5 requires opening once on OpenBox 2.1 before upgrading to 3.0.
- **Removed legacy surfaces:** `openbox --rollback-library` is removed (exits 4 without touching files; use OpenBox 2.1.0 to restore a `library.v6.backup.json`). Plugin API v1 is removed; plugins must declare `"api_version": 2`.
- **Python 3.11+ only for source runs and Python plugins:** Prebuilt 3.0 release packages run natively on `openbox-server` without Python. Running from Python source (`OPENBOX_BACKEND=python`) or executing `.py` v2 plugins requires Python 3.11 or newer.

<details>
<summary><strong>Technical contract matrix (2.1.0 vs 3.0.0)</strong></summary>

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

Background jobs, metadata sync, match preview, and launch audit now also share one unified lifecycle state machine table across the server and UI.

</details>

## Known Limits

- The macOS bundle is ad-hoc signed, so first launch and updates require Privacy & Security confirmation (`System Settings > Privacy & Security > Open Anyway`).
- Linux-only features (such as Steam Game Mode / Gamescope and Flatpak management) remain hidden on Windows and macOS by design.
- OpenBox 1.19 cannot read a schema 7 library.
- Flathub submission is tracked separately.
