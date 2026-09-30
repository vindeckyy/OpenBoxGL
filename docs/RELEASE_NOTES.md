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

Deck Builder for Game Night is coming in 1.16. The first signed emulator-definition pack is published separately, until then the panel says no pack is available yet.

## Fixed

- **Light theme opened dark.** OpenBox now remembers your theme and applies it before the window paints.
- **The page could fail to load on some browsers.** A circular import could leave the library empty on a cold start; it's gone.
- **Reduced-motion users still saw motion.** The Game Night wheel, the constellation view and the page header ignored the setting.
- **The constellation kept working after you closed it.** It now stops.
- **Lightbox arrows jumped when pressed**, and busy buttons went blank while working.
- **The window clipped between 1101 and 1119 pixels wide.**

---

Full changelog: https://github.com/vindeckyy/OpenBoxGL/compare/v1.14.0...v1.15.0
