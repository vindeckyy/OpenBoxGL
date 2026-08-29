---
name: OpenBox
description: Local-first game library and launcher for Linux
colors:
  bg: "#11100e"
  topbar: "#171513"
  panel: "#1b1916"
  panel2: "#24211d"
  line: "#3d3932"
  text: "#f4efe6"
  muted: "#aaa094"
  cyan: "#72c9d4"
  green: "#8fbd8d"
  surface-deep: "#141311"
  surface-card: "#211e1a"
  surface-field: "#27231e"
  surface-hover: "#342d23"
  border-control: "#4b4338"
  border-card: "#534a3d"
  brand: "#f06000"
  focus: "#f06000"
  active: "#f06000"
  action: "#e08a3c"
  action-ink: "#1c160d"
  white: "#ffffff"
  gold: "#e5b65c"
  rating: "#ef8c38"
  launch-shadow: "#e08a3c44"
  danger: "#743f3f"
  mark-start: "#f0c36a"
  mark-end: "#ba593d"
  mark-ink: "#1c160d"
  cover-title-start: "#51412d"
  empty-action: "#e08a3c"
  achievement: "#eaa54f"
  lifecycle-bg: "#45351d"
  bigbox-bg: "#30261a"
  bigbox-copy: "#d0c0a5"
  overlay-insight-cell-0: "#1c1915"
  overlay-insight-cell-1: "#4a2c0a"
  overlay-insight-cell-2: "#8a4f10"
  overlay-insight-cell-3: "#c97316"
  overlay-insight-cell-4: "#f06000"
  border-insight: "#3d3932"
  shadow-insight: "#00000066"
  surface-insight-card: "#1b1916"
  focus-ring: "#f06000"
typography:
  display:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(34px, 6vw, 78px)"
    fontWeight: 900
    lineHeight: 1
    letterSpacing: "normal"
  headline:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 900
    lineHeight: 1.05
    letterSpacing: "normal"
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 800
    lineHeight: 1.2
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 800
    lineHeight: 1.25
    letterSpacing: "0.08em"
  brand:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 800
    lineHeight: 1.2
  nav:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8125rem"
    lineHeight: 1.25
  micro:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "9px"
    lineHeight: 1.25
  meta:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12px"
    lineHeight: 1.4
  body-small:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    lineHeight: 1.4
  action:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    lineHeight: 1.25
  dialog:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "16px"
    lineHeight: 1.2
  title-large:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    lineHeight: 1.2
  subtitle:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "19px"
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
    backgroundColor: "{colors.action}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.pill}"
    padding: "10px"
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-secondary:
    backgroundColor: "{colors.surface-card}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "7px 10px"
  input-field:
    backgroundColor: "{colors.surface-field}"
    textColor: "{colors.white}"
    rounded: "{rounded.xs}"
    padding: "7px 8px"
  card-cover:
    backgroundColor: "{colors.surface-card}"
    textColor: "{colors.text}"
    rounded: "{rounded.cover}"
    padding: "12px"
---

# Design System: OpenBox

## Overview

**Creative North Star: "The Digital Game Room"**

OpenBox is a dense, dark library interface built for moving between a large collection and the next launch. The default surface uses warm dark brown-tan panels, brand-orange focus signals, and an action-orange launch action. A horizontal command rail sits above a three-column workspace: filters on the left, a cover grid in the center, and selected-game detail on the right.

The visual voice is focused and technical with game-room energy. Covers, jewel-case treatments, fullscreen Big Box layouts, and cinematic backdrops give the catalog a playful edge without turning the management UI into generic enterprise software. The base system is the default reference; stock CSS themes intentionally override palette, type stacks, and surface treatment while preserving the shared interaction structure.

**Key Characteristics:**
- Dense three-column workspace with a sticky library header.
- Warm dark tonal layering with small, legible controls.
- Brand-orange focus and selection signals; the same warm ramp drives the launch action.
- Cover art is the main browsing surface and detail metadata stays close.
- Big Box is a fullscreen extension of the same system, not a separate brand.

## Colors

The palette is a unified warm ramp built from the OpenBox logo orange (`#f06000`): one hue family — espresso darks, cream text, and the logo ramp — carries focus, action, rating, and achievement accents. Neutral surfaces cover most of the screen; the warm ramp and its cyan complement identify state and action.

### Primary
- **Brand Orange** (`{colors.brand}` / `{colors.focus}` / `{colors.active}`): The logo orange (`#f06000`, 24deg) marks focused fields, active platform markers, selected covers, and navigation feedback.
- **Action Orange** (`{colors.action}`): Primary buttons and the Play/launch action, with other explicit confirmation actions.
- **Rating** (`{colors.rating}`): Star and rating signals.
- **Achievement** (`{colors.achievement}`): Achievement and unlock accents.
- **Gold Accent** (`{colors.gold}`): Decorative gold for status badges and celebratory details.
- **Cyan Complement** (`{colors.cyan}`): Cool counterpoint to the warm ramp for informational accents.

### Neutral
- **Night Ink** (`{colors.bg}`): Page canvas and deepest fullscreen surfaces.
- **Topbar Ink** (`{colors.topbar}`): Command rail background.
- **Panel** (`{colors.panel}`): Sidebar, details pane, dialogs, and lifecycle surfaces.
- **Raised Panel** (`{colors.panel2}`): Secondary raised surfaces.
- **Card Surface** (`{colors.surface-card}`): Detail cards, emulator items, result rows, and history items.
- **Field Surface** (`{colors.surface-field}`): Form controls and secondary buttons.
- **Hover Surface** (`{colors.surface-hover}`): Hover and selected rows.
- **Divider** (`{colors.line}`): Borders and separators that define the workspace without bright rules.
- **Primary Text** (`{colors.text}`): Titles and high-priority content.
- **Muted Text** (`{colors.muted}`): Metadata, labels, helper copy, and inactive navigation.

### Named Rules
**The State-Color Rule.** Brand orange identifies where the user is focused or selected; action orange identifies where the user can launch or confirm. Do not swap those roles.

**The Dark-Stage Rule.** Keep the neutral surfaces dark enough for cover art, brand-orange focus rings, and action-orange launch actions to remain the first readable signals.

## Typography

**Display Font:** Inter, with `ui-sans-serif`, `system-ui`, and `sans-serif` fallbacks.

**Body Font:** Inter, with `ui-sans-serif`, `system-ui`, and `sans-serif` fallbacks.

**Label/Mono Font:** No distinct mono face is defined in the base system. Labels use the same sans family with uppercase tracking.

**Character:** The base type system is compact, assertive, and easy to scan at small sizes. Stock themes may replace the base stack; Midnight Circuit, for example, uses Syne for display-like labels and Manrope for body copy, while Cinema Marquee uses Bebas Neue for headings.

### Hierarchy
- **Display** (900, `clamp(34px, 6vw, 78px)`, 1): Lifecycle and screensaver statements.
- **Headline** (900, `24px`, 1.05): Selected-game hero titles and prominent detail content, usually uppercase.
- **Title** (800, `18px`, 1.2): Library headings and primary pane titles.
- **Body** (400, `14px`, 1.4): Default application copy, metadata, and form content.
- **Label** (800, `11px`, 1.25, `0.08em`, uppercase): Section labels, field labels, and compact navigation categories.

### Named Rules
**The Scan-First Rule.** Use weight, case, and spacing to make labels and state readable before adding decoration.

## Layout

The application fills the viewport as a vertical shell. The topbar is a horizontally scrollable command rail with a minimum height of `3rem`. Below it, the main workspace uses `190px minmax(520px, 1fr) 410px`: a filter sidebar, a scrollable library, and a selected-game detail pane. At widths up to `1100px`, the columns tighten to `150px 1fr 340px`; below `760px`, the workspace stacks the sidebar, library, and details so handheld users can scroll the full surface.

The library is the visual center. Its sticky header keeps the current collection title, sort, image group, and view actions available while the cover grid scrolls. The base grid uses auto-filled columns with a minimum cover width of `132px`, `16px` horizontal gaps, and `20px` row gaps. The detail pane uses a hero image, a full-width launch action, compact metadata facts, and stacked cards.

Dialogs use a constrained centered surface with a two-column form grid; wide fields span both columns. Big Box switches to a fixed fullscreen three-row composition with a two-column stage, large cover treatment, controller-oriented footer hints, and separate menu or pause overlays.

## Elevation & Depth

The system is layered and ambient. Depth comes first from dark tonal surfaces and gradients, then from backdrop blur on the sidebar, detail pane, sticky header, topbar, and dialogs. Shadows are strongest under covers, fullscreen panels, and dialogs. Amber rings and borders appear on focus and active states rather than as permanent decoration.

### Shadow Vocabulary
- **Topbar ambient** (`0 2px 12px #0008`): Separates the command rail from the workspace.
- **Cover lift** (`0 8px 18px #0007`): Keeps cover cards readable against the library field.
- **Selected cover lift** (`0 0 0 2px #f0600044, 0 10px 23px #000a`): Combines a brand-orange halo with stronger separation.
- **Detail pane separation** (`border-left:1px solid var(--line)`): Keeps the right pane distinct from the grid.
- **Dialog depth** (`0 30px 80px #000c`): Anchors modal work above the dimmed workspace.

### Named Rules
**The Ambient-By-Default Rule.** Use tonal layering and restrained shadows at rest; reserve bright glow and stronger lift for focus, selection, and launch states.

## Shapes

The base form language uses compact corners with a small range from `2px` to `12px`. Inputs and compact controls are nearly square at `3px` to `4px`; detail cards and Big Box panels use `6px` to `12px`; primary launch actions use pill geometry at `18px` or `28px`. Borders are thin and warm, usually one pixel, and clipping is common on cover art and media.

Game covers keep the aspect ratio of each image: portrait, square, and landscape box art all render uncropped. Games without artwork fall back to a portrait `0.72` box with the title centered. Big Box cover treatments use thicker brand-orange borders and occasional jewel-case perspective, while ordinary library cards stay flatter and smaller.

## Components

### Buttons
- **Shape:** Secondary controls use compact `4px` corners; launch controls use a pill silhouette (`18px` in the base library).
- **Primary:** The action-orange Play button is full-width in the detail pane, bold, dark-ink text, and shadowed. Dialog confirmation buttons use the same action-orange surface with dark text.
- **Hover / Focus:** Secondary controls shift to a lighter raised panel. Focused fields use the brand-orange border plus a one-pixel ring; selected library controls use a brand-orange border and lift.
- **Secondary / Ghost / Tertiary:** Topbar menu buttons are borderless and transparent at rest, gaining a raised background on hover.

### Chips
- **Style:** Rating and status values use small muted surfaces with compact text; achievement and ESRB signals use their existing semantic colors.
- **State:** Chips stay subordinate to cover art and the launch action. Selection is communicated by the surrounding control or border.

### Cards / Containers
- **Corner Style:** Cover cards use `5px` base corners; detail and utility cards use `6px` to `8px`.
- **Background:** Library cards use a dark gradient or raised panel; detail cards use the card surface.
- **Shadow Strategy:** Resting covers use cover lift; hover and selection add brand-orange separation and stronger lift.
- **Border:** One-pixel warm borders are the default. Active cards use the brand-orange active border.
- **Internal Padding:** Compact rows use `7px` to `10px`; detail cards use `12px`; fullscreen panels use `24px` to `26px`.

### Inputs / Fields
- **Style:** Fields use a dark raised surface, one-pixel border, `3px` radius, and `7px 8px` padding.
- **Focus:** The border changes to brand orange and gains a one-pixel focus ring.
- **Error / Disabled:** Disabled actions reduce opacity and use a not-allowed cursor; the base system defines a muted danger surface for destructive rows.

### Navigation
- **Style:** The topbar is a compact uppercase command rail. Sidebar platform rows are full-width, left-aligned, and text-first.
- **Default / Hover / Active:** Inactive navigation is muted and transparent. Hover gains a raised background. Active platform rows use a darker panel and a small brand-orange marker.
- **Responsive treatment:** The rail scrolls horizontally; the workspace columns tighten at `1100px` and stack below `760px`. Mobile controls use larger touch targets.

### Big Box
Big Box is the signature fullscreen component. It enlarges the same cover, title, action-orange launch button, brand-orange active border, dark panels, and muted navigation hints for controller and handheld use. Stage, Hybrid, and CoverFlow layouts vary composition while keeping the same state colors and material language.

## Do's and Don'ts

### Do:
- **Do** keep the base surface dark and let brand-orange focus or action-orange launch states carry the strongest chroma.
- **Do** keep labels compact, uppercase, and tracked when they identify sections or navigation categories.
- **Do** use cover art as the browsing anchor and keep metadata close to the selected game.
- **Do** preserve the three-pane workspace and fullscreen Big Box relationship when adding a surface.
- **Do** treat stock CSS themes as intentional overrides of the same interaction system.

### Don't:
- **Don't** introduce a bright neutral page background that competes with cover art.
- **Don't** use the action orange for ordinary selection or change the launch color to a different hue without a theme or product decision.
- **Don't** replace the dense library workflow with a generic dashboard of oversized cards.
- **Don't** add a new type family or palette role to the base system without a theme or product decision.
- **Don't** use permanent glow or deep shadows on every component; reserve them for state and elevation.
