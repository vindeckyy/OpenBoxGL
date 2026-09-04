# ADR 0027: Library Constellation (relationship graph)

**Date:** 2026-09-03
**Status:** Accepted

## Context

M3 asks for a "wow" visualization of the library as a relationship graph. It should reveal clusters by series, developer, genre, platform family, and co-play history without requiring external services.

## Decision

1. **Additive GET route**: `GET /api/v2/library/constellation?kinds=...&limit=...` returns a capped, deterministic graph. `s`/`t` are indices into the `nodes` array to keep the payload small.
2. **Node cap before pairing**: games are ranked by playtime, favorites, and play count, then only the top `limit` (default 400, range 50–1000) are connected. This makes the pairwise scan O(limit²) and keeps the 20,000-game case instant because pairing never happens on the full library.
3. **Single strongest edge per pair**: for each pair of selected games, the highest-weight shared attribute wins; co-play can win if it produces the highest weight. This keeps the graph readable and avoids visual noise.
4. **Static platform family map**: `pkg/parity/parity_constellation.py` uses a curated `PLATFORM_FAMILIES` map, falling back to `"Other"`. Marked `ponytail:` — the upgrade path is to derive families from `platform_categories` settings once that taxonomy exists.
5. **Client-side force simulation**: `static/constellation.js` runs an Archimedean-spiral init with spring-electric ticks on `requestAnimationFrame`, capped to a few hundred nodes and alpha decay. Pan/zoom/click are supported, hover shows a tooltip, and clicking a node dispatches `app:show-game` to reuse the existing selection path. Upgrade path = web worker or Barnes-Hut for >500 nodes.
6. **Color tokens per edge kind**: six `--constellation-edge-*` tokens are added to `app.css :root` and all five theme files; the renderer uses them.

## Consequences

- No new persistent state; the graph is computed on demand from `games` and `history`.
- Co-play is bounded by a 7-day window and capped at 5 shared sessions per edge.
- Big real libraries remain responsive because only the ranked subset is laid out.

## Amendment 2026-09-04 (release QA)

First-look screenshots showed a blank canvas with raw kind labels. Three renderer defects, all fixed in `static/constellation.js`:

1. **Canvas ignores CSS `var()`**: invalid `fillStyle`/`strokeStyle` assignments are silently dropped (black kept), so nodes and edges were invisible on dark themes. Tokens are now resolved through `getComputedStyle` once per render, with keyword fallbacks.
2. **Layout explosion**: unclamped spring-electric steps (repulsion up to ~7500 px/tick for close pairs) teleported every node off-canvas on the first tick. Per-tick displacement is clamped to 24 px and repulsion corrected to Fruchterman-Reingold `k²/d` so pairs settle near distance `k`.
3. **Frozen kind labels**: the label map evaluated `t()` at module load, before the async locale arrived. Labels resolve lazily on every dialog open; the loading indicator now hides after a successful render.
4. **Edgeless drift**: graphs with no shared attributes have no attraction, so repulsion scattered nodes thousands of px apart. Positions clamp into the largest circle fitting the viewport.

Regression cover: `scripts/ui_smoke.cjs` opens the dialog and asserts non-blank canvas pixels, translated labels, and a hidden loading indicator.
