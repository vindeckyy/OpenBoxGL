# Flathub submission checklist

OpenBox 1.11.0 keeps the release-gated x86_64 Flatpak path and maintains the
manifest/AppStream metadata ready for a future Flathub submission. **Submission
itself remains a maintainer decision** (ADR 0013); this list tracks the manual
steps that remain when that decision is made.

## Complete in 1.11.0 (prep and release gate)

- [x] Manifest runtime bumped `org.gnome.Platform 46` → `49` (GNOME 48 EOL'd 2026-03-24; 49 rides freedesktop 25.08 and still ships `webkit2gtk-4.1`).
- [x] `scripts/validate_flatpak_manifest.py` and `tests/test_packaging.py::test_flatpak_manifest` updated to `'49'`.
- [x] AppStream `<content_rating type="oars-1.1">` added (`social-info=mild`; the launcher itself ships no mature content).
- [x] AppStream `<developer id="io.openboxgl">` and `<screenshots>` block added (four 1920×1080 16:9 screenshots).
- [x] `openbox.metainfo.xml` release history current through 1.11.0.
- [x] `scripts/capture_readme_screenshots.py` already produces 1920×1080 16:9 screenshots (no extension needed).
- [x] The v1.11.0 release workflow established the x86_64 bundle gate against GNOME Platform/SDK 49.

## Remaining manual steps (at submission time)

- [x] **Screenshot hosting.** `openbox.metainfo.xml` now points all four screenshots at immutable raw GitHub URLs for the 1.11.0 tag. The docs-site mirror is useful for the README but is not the Flathub submission source of truth; keep these raw URLs or move the images into the Flathub metadata repository when submitting.
- [ ] **Runtime re-verify.** Re-confirm `org.gnome.Platform//49` still ships `webkit2gtk-4.1` (the native host builds with `pkg-config webkit2gtk-4.1`). The CI `flatpak-validate` job's `flatpak-builder --dry-run` is the gate; if a future GNOME runtime drops the GTK3 WebKit, either pin the last runtime that has it or build webkit2gtk-4.1 in the manifest.
- [ ] **Flathub repo setup.** Create `flathub/io.openbox.GameLauncher`, add the maintainers, transfer the manifest + screenshots.
- [ ] **flathubbot PR.** Open the initial submission PR; flathubbot runs validation and review.
- [ ] **AppStream review.** Address reviewer notes on screenshots, metadata, and branding (the "unrelated to the Openbox window manager" disclaimer is already in the description).
- [ ] **First submission cadence.** Agree on a release cadence with Flathub (AppImage remains the canonical artifact channel per ADR 0013).
- [x] **Flatpak bundle gating for 1.11.0.** `release-flatpak.yml` built and published the x86_64 bundle against runtime 49. Repeat this check for each future release tag.

## Explicitly out of scope

- Flathub **store submission** (ADR 0013:52) — producing the bundle is separate from publishing to Flathub.
- Themes marketplace, Premium cloud library, Windows/macOS — ruled out in `docs/PARITY.md`.
