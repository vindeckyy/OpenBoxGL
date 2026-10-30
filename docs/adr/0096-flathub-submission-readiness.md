# ADR 0096: Flathub submission readiness

Status: Accepted as policy. The submission is prepared, not opened.

## Context

The Flatpak bundle is built in release, but store submission is a separate, externally reviewed decision. The 2.0 plan asked for a manifest and metadata that pass review, without promising acceptance.

## Decision

- 2.0.0 is described as "submission-ready", not "available on Flathub".
- The manifest, AppStream metadata, and screenshots are kept current for the release. Screenshots point at a tag that exists.
- The Flathub pull request is opened only with explicit maintainer authorization (plan decision D-03).
- The runtime remains `org.gnome.Platform//49`; `webkit2gtk-4.1` availability is re-verified by the flatpak-validate job.

## Consequences

- Flathub review timing and outcome are outside the release's control.
- The checklist in `docs/flathub-checklist.md` records the status of each item.

## Verification

`docs/flathub-checklist.md`; the flatpak-validate CI job. Not executed by this change.
