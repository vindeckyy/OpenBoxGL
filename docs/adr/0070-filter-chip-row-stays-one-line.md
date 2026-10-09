# ADR 0070: The filter chip row stays one line

**Date:** 2026-10-08
**Status:** Accepted (1.16.1)

## Context

A search that matches words becomes filter chips in `#queryChips`, above the library's side panels.
The row grew with the chips: three chips wrapped to 192 px and pushed the drop zone and the Play
Insights panel down by 145 px. The browser's layout-shift measure counted that as the page moving,
at 0.07 to 0.10 for a search that empties the grid, near the 0.1 threshold web-vitals calls good.
The shift came from the row, not from the grid emptying: the grid did not move.

## Decision

- `#queryChips` is one line with a fixed height (`min-height` and `max-height` of 2rem) that takes a
  full line of the library header. Its actions and chips keep their natural width (the sidebar's
  `.platform` buttons are `width:100%` and would otherwise each fill the row). Items that do not fit
  scroll sideways (`overflow-x: auto`) instead of wrapping.
- The row keeps its height whether it holds no chips or many, so nothing below it moves when a search
  starts or ends.
- The cost is one reserved line under the library title when no search is active, and the sort, view
  and grouping menus sit on the lines below it. Both are accepted for a stable layout.

## Evidence

- A browser probe at 1,000 games: the drop zone stays at 342 px and the Play Insights panel at 423 px
  when the search adds three chips. Before the change they moved to 487 px and 568 px.
- `scripts/perf_browser.cjs` (four runs each at 1,000 and 10,000 games): the search layout shift is 0 on
  every run, including the cold first run. Before: 0.0994 on the first run at both sizes. The earlier
  "0.07 after the chip-width change" result was the same row, sized differently, and is superseded.
- Reliability row 84 (manual) holds the procedure; `ui_smoke`'s `motion.cls` (row 72) still gates the total.

## Consequences

- A long filter list scrolls horizontally in one line; it does not add height. Keyboard users can reach
  the chips with Tab, and the scroll position follows the focused chip.
- Any future change that adds height to this row needs its own measurement, not a reserved gap.
