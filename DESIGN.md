---
name: OpenBox
description: Local-first game library and launcher for Linux
colors:
  ink: "#0d1018"
  topbar: "#11141d"
  panel: "#171b29"
  panel-raised: "#202536"
  card: "#1c2131"
  field: "#24293a"
  line: "#30364a"
  text: "#f3f5fb"
  muted: "#8e96aa"
  cyan: "#25b7e8"
  focus: "#35a9d5"
  active: "#45c4ef"
  green: "#08bf20"
  action: "#21aeda"
  action-ink: "#07131a"
  white: "#ffffff"
  mark-start: "#ffbf30"
  mark-end: "#f27022"
  mark-ink: "#18100a"
  cover-title-start: "#313c5a"
  empty-action: "#2aaddb"
  achievement: "#e8ba41"
  lifecycle-bg: "#283651"
  lifecycle-kicker: "#55c7ee"
  bigbox-bg: "#26334f"
  bigbox-copy: "#aeb8ca"
typography:
  display:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(34px, 6vw, 78px)"
    fontWeight: 900
    lineHeight: 1
    letterSpacing: "normal"
  headline:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 900
    lineHeight: 1.05
    letterSpacing: "normal"
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 800
    lineHeight: 1.2
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "9px"
    fontWeight: 800
    lineHeight: 1.25
    letterSpacing: "0.1em"
  brand:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 800
    lineHeight: 1.2
  nav:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.6875rem"
    lineHeight: 1.25
  micro:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "8px"
    lineHeight: 1.25
  meta:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "10px"
    lineHeight: 1.4
  body-small:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "11px"
    lineHeight: 1.4
  action:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    lineHeight: 1.25
  dialog:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    lineHeight: 1.2
  title-large:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "16px"
    lineHeight: 1.2
  subtitle:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    lineHeight: 1.2
  fullscreen-heading:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(38px, 5vw, 72px)"
    lineHeight: 0.95
  screensaver:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(44px, 8vw, 110px)"
    lineHeight: 0.9
  panel-title:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "28px"
    lineHeight: 1.1
rounded:
  xs: "3px"
  sm: "4px"
  md: "6px"
  lg: "8px"
  xl: "10px"
  pill: "18px"
  hairline: "2px"
  cover: "5px"
  panel: "12px"
  pill-large: "28px"
spacing:
  xs: "2px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  2xl: "25px"
components:
  button-launch:
    backgroundColor: "{colors.green}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.pill}"
    padding: "9px"
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-secondary:
    backgroundColor: "{colors.field}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "7px 10px"
  input-field:
    backgroundColor: "{colors.field}"
    textColor: "{colors.white}"
    rounded: "{rounded.xs}"
    padding: "7px 8px"
  card-cover:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "12px"
---

# Design System: OpenBox

## Overview

**Creative North Star: "The Digital Game Room"**

OpenBox is a dense, dark library interface built for moving between a large collection and the next launch. The default surface uses blue-black panels, bright cyan focus signals, and green launch actions. A horizontal command rail sits above a three-column workspace: filters on the left, a cover grid in the center, and selected-game detail on the right.

The visual voice is focused and technical with game-room energy. Covers, jewel-case treatments, fullscreen Big Box layouts, and cinematic backdrops give the catalog a playful edge without turning the management UI into generic enterprise software. The base system is the default reference; stock CSS themes intentionally override palette, type stacks, and surface treatment while preserving the shared interaction structure.

**Key Characteristics:**
- Dense three-column workspace with a sticky library header.
- Blue-black tonal layering with small, legible controls.
- Cyan focus and selection signals; green means launch or active play.
- Cover art is the main browsing surface and detail metadata stays close.
- Big Box is a fullscreen extension of the same system, not a separate brand.

## Colors

The palette is a blue-black stage with cool text, electric cyan focus, and a green launch signal. Neutral surfaces carry most of the screen; saturated colors identify state and action.

### Primary
- **Electric Cyan** (`{colors.cyan}`): Focus, active platform markers, selected covers, and navigation feedback.
- **Launch Green** (`{colors.green}`): The primary Play action and successful launch state.
- **Action Cyan** (`{colors.action}`): Dialog saves and other explicit confirmation actions.

### Neutral
- **Night Ink** (`{colors.ink}`): Page canvas and deepest fullscreen surfaces.
- **Topbar Ink** (`{colors.topbar}`): Command rail background.
- **Panel** (`{colors.panel}`): Sidebar, details pane, dialogs, and lifecycle surfaces.
- **Raised Panel** (`{colors.panel-raised}`): Inputs, controls, related items, and compact cards.
- **Card Surface** (`{colors.card}`): Detail cards, emulator items, result rows, and history items.
- **Field Surface** (`{colors.field}`): Form controls and secondary buttons.
- **Divider** (`{colors.line}`): Borders and separators that define the workspace without bright rules.
- **Primary Text** (`{colors.text}`): Titles and high-priority content.
- **Muted Text** (`{colors.muted}`): Metadata, labels, helper copy, and inactive navigation.

### Named Rules
**The State-Color Rule.** Cyan identifies where the user is focused; green identifies where the user can launch or is actively playing. Do not swap those roles.

**The Dark-Stage Rule.** Keep the neutral surfaces dark enough for cover art, cyan focus rings, and launch actions to remain the first readable signals.

## Typography

**Display Font:** Inter, with `ui-sans-serif`, `system-ui`, and `sans-serif` fallbacks.

**Body Font:** Inter, with `ui-sans-serif`, `system-ui`, and `sans-serif` fallbacks.

**Label/Mono Font:** No distinct mono face is defined in the base system. Labels use the same sans family with uppercase tracking.

**Character:** The base type system is compact, assertive, and easy to scan at small sizes. Stock themes may replace the base stack; Midnight Circuit, for example, uses Syne for display-like labels and Manrope for body copy.

### Hierarchy
- **Display** (900, `clamp(34px, 6vw, 78px)`, 1): Fullscreen lifecycle and screensaver statements.
- **Headline** (900, `22px`, 1.05): Selected-game hero titles and prominent detail content, usually uppercase.
- **Title** (800, `1.0625rem`, 1.2): Library headings and primary pane titles.
- **Body** (400, `0.75rem`, 1.4): Default application copy, metadata, and form content.
- **Label** (800, `9px`, 1.25, `0.1em`, uppercase): Section labels, field labels, and compact navigation categories.

### Named Rules
**The Scan-First Rule.** Use weight, case, and spacing to make labels and state readable before adding decoration.

## Layout

The application fills the viewport as a vertical shell. The topbar is a horizontally scrollable command rail with a minimum height of `2rem`. Below it, the main workspace uses `170px minmax(520px, 1fr) 410px`: a filter sidebar, a scrollable library, and a selected-game detail pane. At widths up to `1100px`, the columns tighten to `150px 1fr 340px`; below `760px`, the workspace stacks the sidebar, library, and details so handheld users can scroll the full surface.

The library is the visual center. Its sticky header keeps the current collection title, sort, image group, and view actions available while the cover grid scrolls. The base grid uses auto-filled columns with a minimum cover width of `118px`, `14px` horizontal gaps, and `17px` row gaps. The detail pane uses a hero image, a full-width launch action, compact metadata facts, and stacked cards.

Dialogs use a constrained centered surface with a two-column form grid; wide fields span both columns. Big Box switches to a fixed fullscreen three-row composition with a two-column stage, large cover treatment, controller-oriented footer hints, and separate menu or pause overlays.

## Elevation & Depth

The system is layered and ambient. Depth comes first from dark tonal surfaces and gradients, then from backdrop blur on the sidebar, detail pane, sticky header, topbar, and dialogs. Shadows are strongest under covers, fullscreen panels, and dialogs. Cyan rings and borders appear on focus and active states rather than as permanent decoration.

### Shadow Vocabulary
- **Topbar ambient** (`0 2px 12px #0008`): Separates the command rail from the workspace.
- **Cover lift** (`0 8px 18px #0007`): Keeps cover cards readable against the library field.
- **Selected cover lift** (`0 0 0 2px #28b9e544, 0 10px 23px #000a`): Combines a cyan halo with stronger separation.
- **Detail pane separation** (`-12px 0 30px #0005`): Keeps the right pane distinct from the grid.
- **Dialog depth** (`0 30px 80px #000c`): Anchors modal work above the dimmed workspace.

### Named Rules
**The Ambient-By-Default Rule.** Use tonal layering and restrained shadows at rest; reserve bright glow and stronger lift for focus, selection, and launch states.

## Shapes

The base form language uses compact corners with a small range from `3px` to `10px`. Inputs and compact controls are nearly square at `3px` to `6px`; detail cards and Big Box panels use `8px` to `12px`; primary launch actions use pill geometry at `18px` or `28px`. Borders are thin and cool, usually one pixel, and clipping is common on cover art and media.

Game covers keep a tall `0.72` aspect ratio. Big Box cover treatments use thicker cyan borders and occasional jewel-case perspective, while ordinary library cards stay flatter and smaller.

## Components

### Buttons
- **Shape:** Secondary controls use compact `4px` corners; launch controls use a pill silhouette (`18px` in the base library).
- **Primary:** The green Play action is full-width in the detail pane, bold, dark-ink text, and shadowed. Dialog confirmation buttons use the cyan action surface with dark text.
- **Hover / Focus:** Secondary controls shift to a lighter raised panel. Focused fields use the focus blue border plus a one-pixel ring; selected library controls use a cyan border and lift.
- **Secondary / Ghost / Tertiary:** Topbar menu buttons are borderless and transparent at rest, gaining a raised background on hover.

### Chips
- **Style:** Rating and status values use small muted slate surfaces with compact text; achievement and ESRB signals use their existing semantic colors.
- **State:** Chips stay subordinate to cover art and the launch action. Selection is communicated by the surrounding control or border.

### Cards / Containers
- **Corner Style:** Cover cards use `5px` base corners; detail and utility cards use `6px` to `8px`.
- **Background:** Library cards use a dark gradient or raised panel; detail cards use the card surface.
- **Shadow Strategy:** Resting covers use cover lift; hover and selection add cyan separation and stronger lift.
- **Border:** One-pixel cool borders are the default. Active cards use the cyan active border.
- **Internal Padding:** Compact rows use `7px` to `10px`; detail cards use `12px`; fullscreen panels use `24px` to `26px`.

### Inputs / Fields
- **Style:** Fields use a dark raised surface, one-pixel border, `3px` radius, and `7px 8px` padding.
- **Focus:** The border changes to focus blue and gains a one-pixel focus ring.
- **Error / Disabled:** Disabled actions reduce opacity and use a not-allowed cursor; no separate error palette is defined in the base system.

### Navigation
- **Style:** The topbar is a compact uppercase command rail. Sidebar platform rows are full-width, left-aligned, and text-first.
- **Default / Hover / Active:** Inactive navigation is muted and transparent. Hover gains a raised background. Active platform rows use a darker panel and a small cyan marker.
- **Responsive treatment:** The rail scrolls horizontally; the workspace columns tighten at `1100px` and stack below `760px`. Mobile controls use larger touch targets.

### Big Box
Big Box is the signature fullscreen component. It enlarges the same cover, title, green launch action, cyan active border, dark panels, and muted navigation hints for controller and handheld use. Stage, Hybrid, and CoverFlow layouts vary composition while keeping the same state colors and material language.

## Do's and Don'ts

### Do:
- **Do** keep the base surface dark and let cyan focus or green launch states carry the strongest chroma.
- **Do** keep labels compact, uppercase, and tracked when they identify sections or navigation categories.
- **Do** use cover art as the browsing anchor and keep metadata close to the selected game.
- **Do** preserve the three-pane workspace and fullscreen Big Box relationship when adding a surface.
- **Do** treat stock CSS themes as intentional overrides of the same interaction system.

### Don't:
- **Don't** introduce a bright neutral page background that competes with cover art.
- **Don't** use green for ordinary selection or cyan for a launch-success state.
- **Don't** replace the dense library workflow with a generic dashboard of oversized cards.
- **Don't** add a new type family or palette role to the base system without a theme or product decision.
- **Don't** use permanent glow or deep shadows on every component; reserve them for state and elevation.
