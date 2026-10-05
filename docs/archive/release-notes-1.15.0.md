# OpenBox 1.15.0 — Finish the surface

The interface gets a real motion system, a readable light theme, and screens for the features that shipped without one. Everything still runs 100% locally — no account, no cloud, no telemetry.

---

## What's New

- **Motion that respects you.** Dialogs now animate out as well as in, theme switches cross-fade, covers reserve their space and fade in instead of jumping, and typing in search no longer replays the whole grid's entrance. If your system asks for reduced motion, everything goes static — one switch, so nothing gets missed.
- **A light theme you can actually read.** Harbor Light had white text on white inputs and cards. It's fixed, and every theme is now checked for readable contrast automatically.
- **Toasts that stay put.** Notifications now sit above dialogs and Big Box instead of hiding behind them, and an Undo can no longer be overwritten by another message.
- **Better for everyone.** Screen readers hear the real size of your library, Big Box announces itself properly, job progress is exposed, touch targets are bigger, and high-contrast system modes are supported.
- **Emulator definition updates.** Settings > Emulators can check for, install and roll back signed community emulator definitions — and never overwrites ones you edited.
- **Time Machine Compare.** Pick two dates and see exactly which games were added, removed or changed between them.
- **Settings > About.** See your version, platform, data folder, and whether OpenBox is running in its own window or a browser tab.
- **Windows uninstaller.** A clean way to remove OpenBox — the install, PATH entry, Start Menu shortcut and `openbox://` link — while leaving your library untouched.
- **Smoother Big Box.** The stage now slides in from the direction you moved.

## Not in this release

Deck Builder for Game Night is coming in 1.16. The first signed emulator-definition pack is published
separately, until then the panel says no pack is available yet.

Everything below was consciously deferred before 1.15.0, not discovered afterwards. Each one is
listed with the reason it was held, so nothing here reads as an oversight — and so 1.16.0 inherits a
checked list rather than a guess.

| Deferred | Why |
|---|---|
| **Deck Builder for Game Night** | Third deferral (1.12 → 1.14.1 → 1.15). Chosen over polish for 1.15; the 1.14.1 plan had said not to defer a third time, and 1.15 accepted that cost deliberately. |
| **Search-worker facets** | Conditional on measurement: `facet_ms_p95` was 1395 ms against a 2000 ms budget at 20k games (1.4× headroom). Only worth building if a long-task probe shows animation jank during a facet recompute. |
| **Presence, saves / play-state sync** | Needs an ADR and a privacy review. `CATALOG_FIELDS` verifiably does not carry these fields today (`parity_library_sync.py`). |
| **Windows storefront importers** (GOG / EA / Ubisoft) | The largest parity gap — Windows discovers only Steam and Epic — but it is L-sized and unrelated to polish, so it leads the 1.16 candidate list. |
| **Per-platform setup checklist, "finish it" shelf, Constellation viewpoints / path / PNG** | Feature work. None of it blocks polish. |
| **Kiosk PIN backoff** | `verify_kiosk_pin` is stateless, so there is nothing to back off against yet. Small, and unrelated to polish. |
| **QR phone remote, `price_paid` cost/hour, 50k performance *gate*** | Already cut in 1.14.1 for the same reasons; re-deferring rather than re-litigating. |
| **Wayland fractional scaling, Steam Deck manual passes** | Need physical maintainer verification that cannot be produced on CI. |
| **GTK minimum window size** | Requires a native-host rebuild, which was a stated non-goal. |
| **macOS** | Out of scope per `SUPPORT.md`. |

## Fixed

- **Light theme opened dark.** OpenBox now remembers your theme and applies it before the window paints.
- **The page could fail to load on some browsers.** A circular import could leave the library empty on a cold start; it's gone.
- **Reduced-motion users still saw motion.** The Game Night wheel, the constellation view and the page header ignored the setting.
- **The constellation kept working after you closed it.** It now stops.
- **Lightbox arrows jumped when pressed**, and busy buttons went blank while working.
- **The window clipped between 1101 and 1119 pixels wide.**

---

Full changelog: https://github.com/vindeckyy/OpenBoxGL/compare/v1.14.0...v1.15.0
