# ADR 0086: platform capability contract

**Date:** 2026-10-28
**Status:** Accepted

## Context

1.19.0 made macOS a first-class channel, so three platforms now share one
runtime. Each feature that only exists on one platform had its own gate:
`data-macos-hidden` and `data-windows-only` markers in `index.html`, raw
`nativeCaps.platform` string checks in `setup.js` and `settings.js`, and
backend checks written as `not IS_WINDOWS` that silently included macOS.

That shape fails in two directions. A control can render on a platform that
cannot serve it, and the user only finds out at click time: 1.19.0 shipped a
gamescope preset control on Windows whose stored value broke every launch.
And a new platform has to edit markup and every JS check instead of declaring
what it supports.

The platform flags in `pkg/platform_compat.py` are authoritative. The gap is
that the frontend never received them, and the backend guards were written
against the wrong complement.

## Decision

1. **The server computes one feature contract.** `platform_features()` in
   `pkg/platform_compat.py` returns a dict of booleans derived from
   `IS_LINUX`, `IS_WINDOWS`, and `IS_MACOS`. `GET /api/native/capabilities`
   returns it as `features`, next to the existing host fields.

2. **The frontend has one generic marker.** Any element may carry
   `data-feature="<key>"`. `applyPlatformVisibility()` in `static/app.js`
   shows the element when the key is `true` and hides it otherwise.
   `<option>` elements are removed instead of hidden because WebKit's select
   menu ignores `hidden`. The legacy `data-macos-hidden` and
   `data-windows-only` markers are gone.

3. **A missing contract means nothing is supported.** If the capabilities
   fetch fails, or an older server omits `features`, every key reads as
   false and every gated control stays hidden. Hidden is the safe failure:
   an absent control cannot break a launch, a visible one can.

4. **Backend guards are positive.** A Linux-only feature checks `IS_LINUX`,
   never `not IS_WINDOWS` (which includes macOS). A capability key and its
   backend guard must agree; `tests/test_capability_contract.py` checks the
   payload and the markup, and the route-guard tests check that gated
   endpoints refuse off-platform instead of running a native tool that does
   not exist.

### The keys

| feature | Linux | Windows | macOS | gates |
|---|---|---|---|---|
| `gamescope` | true | false | false | preset override, Game Mode, window marking |
| `mangohud` | true | false | false | MangoHud overlay toggle |
| `handheld_perf` | true | false | false | ryzenadj TDP controls |
| `flatpak` | true | false | false | emulator install/update-all, folder grants |
| `wine` | true | false | false | Wine/Proton manager |
| `faugus` | true | false | false | Faugus import source |
| `lutris` | true | false | false | Lutris import source |
| `heroic` | true | true | false | Heroic import source |
| `epic` | true | true | false | Epic-through-Heroic import |
| `native_storefronts` | false | true | false | direct GOG/EA/Ubisoft import |
| `desktop_entry` | true | false | false | Install desktop shortcut |

Heroic ships on Windows, so `heroic` and `epic` are the two source keys that
are true on more than one platform. `native_storefronts` is the only key that
is true on Windows and false on Linux: those importers read Windows launchers.

## Consequences

- New platform-specific controls declare a feature instead of editing markup
  logic per OS. A new key is added in `platform_features()`, the ADR table,
  and the contract test's `ALL_FEATURES`.
- Controls that should exist but are off by default keep a literal `hidden`
  attribute; the visibility pass reveals them when the feature is true.
  `native_storefronts` controls use this so a failed fetch leaves them hidden.
- The settings keys themselves do not change. `gamescope_preset`,
  `mangohud_enabled`, `apply_perf`, and the custom presets stay in
  `KNOWN_SETTINGS` on every platform; they are now gated controls and
  no-ops off Linux rather than removed schema.
- `GET /api/native/capabilities` grows one field; the v1 route contract
  freezes routes, not response fields.
- Tests: `tests/test_capability_contract.py` pins the three-platform payload,
  the markup attribute set, and the JS usage set. `tests/test_native_handlers.py`
  pins the payload and the empty-fallback shape.
