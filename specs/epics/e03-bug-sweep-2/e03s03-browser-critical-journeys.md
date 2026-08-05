# e03s03 — Critical browser journey sweep

## 1. Identity

- **Story:** e03s03
- **Type:** browser QA
- **Risk:** P1
- **BCPs:** 5
- **Wave:** 2

## 2. User value

Real-browser evidence for initial load, settings round-trip, storefront partial save, favicon, launch, and update check over the actual UI, plus network/console diagnostics.

## 3. Context

`agent_browser` is not on PATH in this environment; the installed Puppeteer under `scripts/node_modules` with `/usr/bin/google-chrome` is the working fallback (same as e01s03). Server runs with a temp `OPENBOX_DATA_DIR`, `OPENBOX_SAFE_MODE=1`, and a generated token.

## 4. Problem statement

A green script suite does not prove the UI works; I12's favicon 404 and I14's poll cap are frontend behaviors only observable in a browser.

## 5. Purpose / callers / contracts

Harness `scripts/e03s03-browser-sweep.mjs` drives the real controls. Evidence lands in `specs/verifications/e03s03-browser-sweep.md` and `e03s03-browser.json` plus screenshots under `specs/verifications/artifacts/`.

## 6. Assumptions

- SAFE_MODE disables plugin hooks only; launches still execute the configured command.
- Synthetic games use `/bin/echo` and `/bin/true` only.

## 7. Exit criteria

All journeys complete; zero console/page errors; zero failed requests; favicon routes 200; final card count unchanged; cleanup proof recorded.

## 13. Verification commands

```bash
node scripts/e03s03-browser-sweep.mjs "$URL" specs/verifications/artifacts
```

## 17. Acceptance criteria

- [ ] Load shows both fixture cards with no errors.
- [ ] Settings round-trip persists 60 and preserves storefront flags; no raw password in the public payload.
- [ ] Storefront partial save persists Steam auto-import and preserves screensaver.
- [ ] `/favicon.svg` and `/favicon.ico` answer 200; no 404 on initial load (I12).
- [ ] Launch click produces no errors; update check reports current version.
- [ ] Server terminated and temp data root removed afterward.

## 18. Verification script (step-by-step)

1. Start the isolated fixture and wait for `server.port`/`server.token`.
2. Run the journey harness; assert the report summary is clean.
3. Inspect the JSON diagnostics and screenshots.
4. Terminate the server and remove the data root; record cleanup proof.
