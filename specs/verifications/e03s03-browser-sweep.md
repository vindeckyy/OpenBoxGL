# e03s03 browser critical journey sweep

Date: 2026-08-04
Browser: headless Chromium via the installed Puppeteer under `scripts/node_modules` (`/usr/bin/google-chrome`; `agent_browser` binary not installed on PATH).
Application data: isolated temporary `OPENBOX_DATA_DIR`; synthetic two-game library; `OPENBOX_SAFE_MODE=1`.
Credentials: generated local token used at runtime only; no real credential stored in evidence.
Raw diagnostics: `specs/verifications/e03s03-browser.json`.
Tooling: `scripts/e03s03-browser-sweep.mjs` (kept as a reusable journey harness).

## Isolated browser fixture

- [x] Loopback server used an ephemeral port.
- [x] Synthetic games used `/bin/echo` (with an explicit `launch` command) and `/bin/true`.
- [x] General/storefront settings used canary values only.
- [x] Native Tk UI was not started concurrently.
- [x] Server PID terminated and temporary data root removed after capture.

## Startup, auth, and library render

status: passing

- [x] Tokenized URL loaded `All games` with 2 cards showing both fixture names.
- [x] Final card count remained two after all journeys.
- [x] No page exception, console error, or failed network transport occurred.

## Settings round-trip

status: passing

- [x] Opened Settings through the real `#settingsButton` control.
- [x] Changed screensaver delay to 60 and submitted the real form.
- [x] GET settings showed `screensaver_seconds: 60`.
- [x] `storefront_auto_import` flags survived the general settings save.
- [x] The public response contained no raw `gameyfin_password` key.

## Storefront partial save

status: passing

- [x] Enabled Steam auto-import through the real `#storefrontAutoImportSteam` control and saved via `#saveStorefront`.
- [x] GET settings showed Steam auto-import true.
- [x] `screensaver_seconds` survived the storefront partial save.

## Favicon (I12)

status: fixed

- [x] `/favicon.svg` and `/favicon.ico` both answer 200 with the repo icon.
- [x] No 404 request appears in the network diagnostics on initial load.

## Launch lifecycle (SAFE_MODE)

status: passing

- [x] Opened a card and clicked the real play control.
- [x] No console/page errors and no failed requests during launch.

## Update check

status: passing

- [x] Update check inside Settings runs and reports `OpenBox 0.7.0 is current.`

## Cleanup proof

- [x] Server process terminated (health probe refuses connection).
- [x] Temporary data root removed.
- [x] Browser processes closed by the harness.

## Notes

- A Puppeteer real-click on `#settingsButton` proved intermittently flaky against layout shifts; the harness uses `element.click()` dispatch with a retry instead. The application dialog itself opens deterministically (verified by direct DOM probe), so this is harness timing, not an application defect.
- The Gameyfin install poll change (I14, `watchGameyfinInstall` cap 40 to 1200 attempts) is a frontend constant; it is covered by code review and the full suite rather than an end-to-end Gameyfin install (no live Gameyfin server in the sweep environment).
