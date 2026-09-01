# ADR 0015: Internationalization System

**Date:** 2026-09-01
**Status:** Accepted

## Context

OpenBox's interface was English-only. The `pkg/parity/parity_premium.py` module contained a `STRINGS` dict with partial translations for Spanish, German, French, and Portuguese, but the frontend JavaScript never consumed these strings. The settings page had a locale selector marked "Localization is planned for a future release."

`docs/PARITY.md` marked Localization as the only remaining "partial" parity row.

## Decision

Implement a full **data-i18n** internationalization system:

1. **Locale files**: JSON files in `locales/{en,es,de,fr,pt}.json` with nested key structure (e.g., `nav.library`, `sidebar.search`, `settings.title`).
2. **HTML translation**: `data-i18n="key"` attributes on translatable elements; `data-i18n-placeholder`, `data-i18n-title`, `data-i18n-aria-label` for attributes.
3. **JS translation**: `t(key, params)` function in `static/i18n.js` with `{placeholder}` interpolation and English fallback.
4. **Locale loading**: `fetch('/locales/{locale}.json')` with `en.json` as the canonical fallback.
5. **Settings integration**: `available_locales` exposed in `public_settings`; locale selector populated dynamically; `setLocale()` re-translates without reload.
6. **Gate**: `scripts/check_i18n.py` verifies 100% key coverage across all locale files and that all `data-i18n`/`t()` keys exist in `en.json`.
7. **Packaging**: `locales/` bundled in AppImage and Flatpak.

The existing `STRINGS` dict in `parity_premium.py` is deprecated; `strings_for()` remains as a shim for backward compatibility.

## Consequences

- **Positive**: Closes the last parity gap — Localization moves from "partial" to "done".
- **Positive**: Adding a new locale requires only a new JSON file and a route entry.
- **Positive**: Gate enforcement prevents key drift between locales.
- **Negative**: Every new UI string requires a key in `en.json` and translations in all 5 locale files.
- **Negative**: Some dynamically generated strings in JS still need manual `t()` calls.

## Alternatives Considered

1. **Use a JS i18n library (i18next, etc.)**: Rejected — violates the dependency-free frontend policy.
2. **Server-side translation**: Rejected — would require template rendering and break the static HTML + JS module architecture.
3. **Keep STRINGS dict approach**: Rejected — it was never wired to the frontend and had incomplete coverage.
