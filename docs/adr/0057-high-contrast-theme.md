# ADR 0057: High-contrast stock theme and contrast-ratio gate

**Date:** 2026-09-16
**Status:** Accepted

## Context

The design system requires every theme to define every `:root` token from
`static/app.css`, and `tests/test_stock_themes.py` already asserted a WCAG AA
contrast floor for `--text`/`--bg` and `--muted`/`--bg`. That floor was never
proven against a maximum-legibility palette, and secondary text tokens
(`--nav-muted`, `--card-muted`, `--section-muted`, `--detail-head`, `--fact-label`,
`--top-status`, `--text-soft`) could regress without a test noticing.

## Decision

- Ship a sixth stock theme, `themes/High Contrast.css`: black surfaces, white
  text, high-luminance accent, every token defined in `:root` and no raw hex
  outside it (same contract as the other five).
- Extend the stock-theme test with a table of text-token/background pairs
  (11 pairs across background, topbar, panel, and card surfaces) and require
  a contrast ratio of at least 4.5:1 for each stock theme. This is the
  contrast-ratio gate; it runs in the default suite, not a separate workflow.
- `stock_themes.ensure_stock_themes` already globs `themes/*.css`, so the new
  theme installs and refreshes like the others. The test that counted five
  themes now counts six.

## Consequences

- A new stock theme or a token change that drops secondary text below AA
  fails the suite.
- High Contrast proves the token contract covers a palette very different
  from the default dark theme; if a component hardcodes a color, it will
  become unreadable there and the token gate already forbids that.
- Themes are CSS-only; no runtime or theme-selection code changed.
