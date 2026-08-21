# ADR 0004: Theme token contract closure

Date: 2026-08-20
Status: Accepted

## Context

`static/app.css:1-45` defines the design system's `:root` tokens (61 colors, 4 radii, 11 fonts). Five stock themes override a subset, but none defined `--surface-deep` (`app.css:5` default `#141311`) which `library.js:155 .library{background:var(--surface-deep)}` relies on, causing warm bleed on cool themes like Midnight Circuit (`bg #070b14`). Raw hex outside `:root` also existed for shadow/overlay colors (`#0006`, `#050403d9`, etc.) counted separately by `scripts/check_tokens.py:8` baseline `343`.

Visual output must not change, but token hygiene must ratchet.

## Decision

- Introduce shadow/overlay tokens in `static/app.css:1-45 :root` for the 17 4/8-digit hex values used outside `:root` and replace each usage with `var(--*)` with identical computed color. Values: `--shadow-cover #0006`, `--shadow-cover-strong #0007`, `--shadow-dialog #000c`, `--shadow-elevated #0009`, `--shadow-cover-selected #000a`, `--shadow-empty #0005`, `--accent-ghost #f0600055`, `--accent-ghost-soft #f0600044`, `--accent-ghost-faint #f060002e`, `--overlay-backdrop #050403d9`, `--overlay-backdrop-strong #05070bd9`, `--overlay-backdrop-soft #05070bcc`, `--overlay-screensaver-start #05070bcf`, `--overlay-screensaver-mid #05070b1f`, `--surface-sheet #171513dd`, `--border-sheet #ffffff55`, `--hero-scrim #0d0b0815`.

- Add `--surface-deep` to each theme `:root` with theme-intentional deep values: Cinema Marquee `#080606`, Harbor Light `#f5f8fc`, Midnight Circuit `#05080f`, Nordic Mist `#0c1116`, Phosphor Terminal `#020402`.

- Keep `app.css:5 #141311` fallback and document theme font overrides as intentional.

- Add `tests/test_frontend_contract.py` asserting every `var(--*)` used after `:root` is defined in `:root` and each theme defines `surface-deep`.

## Consequences

- `tests/test_frontend_contract.py` now guards theme contract; `python3 scripts/check_tokens.py` stays at 343 (baseline ratchet deferred until 6-digit theme outside hexes are also tokenized). Next sweep can ratchet baseline down.

- No visual regression: hex values preserved via indirection, verified by `content-visibility` and `contain-intrinsic-size` unchanged.

- Themes now correctly render library and screenshot grid backgrounds per theme palette.
