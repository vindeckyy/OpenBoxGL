# ADR 0034: Big Box video snaps

**Date:** 2026-09-04
**Status:** Accepted

## Context

Big Box mode (default "stage" view) renders a static cover image for the focused game. LaunchBox and Playnite both support video snaps — short looping gameplay videos that play when a game is selected in the carousel. OpenBox already stores `video_snap`, `video_theme`, `video_trailer`, and `video_recording` fields and has an `active_video()` helper, but Big Box never uses them.

## Decision

Add video snaps to Big Box default (stage) mode with minimal resource usage:

1. **Single reused `<video>` element**: rather than creating video nodes per game, one `<video>` element is created on demand and appended to the `.bigbox-cover` container. When the focused game changes, the existing element is paused and its `src` is cleared before the new one loads.

2. **600ms debounce**: `scheduleVideoSnap(game)` sets a timer; if the user moves to another game within 600ms, the timer is cancelled. This prevents thrashing when scrolling rapidly through the carousel.

3. **BGM ducking**: while a video snap plays, `AppState.libraryBgm.volume` is lowered to 0.1. When the video is cleared, volume is restored to the user's setting (0.35 if `video_bgm_mix` is on, 0.6 otherwise).

4. **Reduced-motion respect**: `prefers-reduced-motion: reduce` disables video snaps entirely — the static cover is shown. CSS also hides `.bigbox-video-snap` under the media query as a second defense.

5. **Static cover fallback**: if no video URL is available for the game, `clearVideoSnap()` is called and the existing static `<img>` cover remains visible.

6. **Mode isolation**: video snaps only play in the default "stage" mode. Hybrid and coverflow modes call `clearVideoSnap()` to ensure no video leaks across mode switches.

## Consequences

- Big Box stage mode now shows looping gameplay videos when available, matching LaunchBox/Playnite behavior.
- No new dependencies — uses the existing `<video>` element and `media()` URL helper.
- Resource usage is bounded: one video element, debounced loading, BGM ducking.
- Reduced-motion users see static covers only.
