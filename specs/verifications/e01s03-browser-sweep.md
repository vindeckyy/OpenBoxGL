# e01s03 critical browser journey sweep

Date: 2026-07-26
Browser: existing Puppeteer 22 headless Chromium fallback after `agent_browser` reported its binary unavailable.
Application data: isolated temporary `OPENBOX_DATA_DIR`; synthetic two-game library; `OPENBOX_SAFE_MODE=1`.
Credentials: generated local token used at runtime only; no token or real credential stored in evidence.
Raw diagnostics: `specs/verifications/e01s03-browser.json`.

## Isolated browser fixture

- [x] Loopback server used an ephemeral port.
- [x] Synthetic games used `/bin/true`, `/bin/echo`, and a bounded `sleep 6` launch command.
- [x] General/storefront settings used canary values only.
- [x] Native Tk UI was not started concurrently.
- [x] Server PID terminated and temporary data root removed after capture.

## Startup, auth, and library

status: fixed

- [x] Tokenized URL loaded `All games` with `2 games` and two cards.
- [x] Final card count remained two after all journeys.
- [x] No page exception or failed network transport occurred.
- [x] Missing/wrong API token behavior was covered in e01s02.

### Settings isolation

status: fixed

- [x] Opened Settings through `#settingsButton`.
- [x] Changed screensaver delay from 90 to 60 and submitted the real form.
- [x] Opened Storefront Manager, enabled Steam auto-import, and saved through the real control.
- [x] GET settings showed `screensaver_seconds: 60` and Steam auto-import true.
- [x] The public response contained no raw `gameyfin_password` key.
- [x] General and storefront changes survived each other's partial saves.

Evidence: `specs/verifications/artifacts/e01s03-settings.png`.

### Launch and session lifecycle

status: fixed

- [x] Selected the first visible game card.
- [x] Toggled favorite through the visible control; label changed to `Remove favorite`.
- [x] Launched a safe six-second fixture command.
- [x] Waited for lifecycle overlay completion and observed the Running control become enabled.
- [x] Opened Running Games and observed the fixture, PID, start time, and bounded controls.
- [x] No duplicate launch or stale-session symptom was observed.

Evidence: `specs/verifications/artifacts/e01s03-launch-session.png`.

### Failure recovery

status: fixed

- [x] Opened Storefront Manager with no Gameyfin URL.
- [x] Triggered Test Gameyfin through the visible control.
- [x] UI rendered `Gameyfin URL is required.` as an actionable terminal error.
- [x] Expected `/api/gameyfin/test` status 400 was recorded; no transport failure or page exception occurred.
- [x] UI remained interactive and later theme/Big Box/settings journeys succeeded.

Evidence: `specs/verifications/artifacts/e01s03-failure-recovery.png`.

### Accessibility and responsive smoke

status: fixed

- [x] Themes dialog exposed Default plus five stock themes.
- [x] Big Box opened and closed with Escape.
- [x] Narrow viewport was 420×800 with zero horizontal document overflow.
- [x] Settings dialog remained open and usable at narrow width.
- [x] Keyboard Tab moved focus to an input.
- [x] Reduced-motion media preference was enabled for the responsive capture.

Evidence: `specs/verifications/artifacts/e01s03-responsive.png`.

## Diagnostics

status: fixed

- page errors: 0
- failed requests at transport level: 0
- HTTP 400: one expected Gameyfin validation response, visibly handled
- HTTP 404: one browser-generated `/favicon.ico` request
- console errors: corresponding browser resource messages for the expected 400 and missing favicon

## Candidates

- id: UI-001
  priority: P3
  area: browser polish
  symptom: every initial page load requests `/favicon.ico`, receives 404, and adds a console resource error
  reproduction_count: 2 browser runs
  status: deferred
  reason: cosmetic/no functional impact; today’s P1/P2 fix budget excludes it
  recommended_action: serve the existing OpenBox icon as favicon or add an explicit icon link in a later patch

The expected Gameyfin 400 is rejected as a defect because the visible UI handled the missing required configuration and remained usable.

## Cleanup proof

- [x] Headless browser closed in a `finally` block.
- [x] OpenBox server PID no longer exists.
- [x] Temporary OpenBox data root was recursively removed.
- [x] Required screenshots and sanitized diagnostics were retained under `specs/verifications/`.
- [x] No real user-data path or external account was mutated.

## Verdict

All planned P0/P1 browser journeys completed with terminal evidence. No browser P1/P2 defect was found. UI-001 is a deferred P3 cosmetic finding. All checklist items are complete.
