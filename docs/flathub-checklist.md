# Flathub submission checklist

OpenBox 1.8.0 prepares the Flatpak manifest and AppStream metadata for Flathub. **Submission itself remains a maintainer decision** (ADR 0013) and is out of scope for 1.8.0; this list tracks the remaining manual steps when that decision is made.

## Done in 1.8.0 (prep)

- [x] Manifest runtime bumped `org.gnome.Platform 46` → `49` (GNOME 48 EOL'd 2026-03-24; 49 rides freedesktop 25.08 and still ships `webkit2gtk-4.1`).
- [x] `scripts/validate_flatpak_manifest.py` and `tests/test_packaging.py::test_flatpak_manifest` updated to `'49'`.
- [x] AppStream `<content_rating type="oars-1.1">` added (`social-info=mild`; the launcher itself ships no mature content).
- [x] AppStream `<developer id="io.openboxgl">` and `<screenshots>` block added (three 1920×1080 16:9 screenshots).
- [x] `openbox.metainfo.xml` release history current through 1.8.0.
- [x] `scripts/capture_readme_screenshots.py` already produces 1920×1080 16:9 screenshots (no extension needed).

## Remaining manual steps (at submission time)

- [x] **Screenshot hosting.** `openbox.metainfo.xml` screenshot URLs now point at the live docs-site paths (`https://openboxgl.github.io/openbox-*.png`); all three resolve with `image/png`. (The original `/assets/` prefix 404'd.) At Flathub submission, prefer moving the screenshots into the app repo and pointing `<image>` at its raw GitHub URLs (the Flathub convention).
- [ ] **Runtime re-verify.** Re-confirm `org.gnome.Platform//49` still ships `webkit2gtk-4.1` (the native host builds with `pkg-config webkit2gtk-4.1`). The CI `flatpak-validate` job's `flatpak-builder --dry-run` is the gate; if a future GNOME runtime drops the GTK3 WebKit, either pin the last runtime that has it or build webkit2gtk-4.1 in the manifest.
- [ ] **Flathub repo setup.** Create `flathub/io.openbox.GameLauncher`, add the maintainers, transfer the manifest + screenshots.
- [ ] **flathubbot PR.** Open the initial submission PR; flathubbot runs validation and review.
- [ ] **AppStream review.** Address reviewer notes on screenshots, metadata, and branding (the "unrelated to the Openbox window manager" disclaimer is already in the description).
- [ ] **First submission cadence.** Agree on a release cadence with Flathub (AppImage remains the canonical artifact channel per ADR 0013).
- [ ] **Flatpak bundle gating.** `release-flatpak.yml` already produces a release-gated bundle; confirm it still builds against runtime 49 before tagging.

## Explicitly out of scope

- Flathub **store submission** (ADR 0013:52) — producing the bundle is separate from publishing to Flathub.
- Themes marketplace, Premium cloud library, Windows/macOS — ruled out in `docs/PARITY.md`.
