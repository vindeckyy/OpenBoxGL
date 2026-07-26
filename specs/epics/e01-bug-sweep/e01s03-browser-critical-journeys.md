# e01s03 — Critical browser journey sweep

## 1. Identity

- **Story:** e01s03
- **Type:** test / user acceptance
- **Risk:** P1
- **BCPs:** 5
- **Context:** browser UI + local API
- **Wave:** 2 — approximately 90 minutes

## 2. User value

Players can complete the most important OpenBox workflows without silent failures, stale UI, accidental settings loss, duplicate actions, inaccessible controls, or unrecoverable error states.

## 3. Context

`index.html` contains 103 named functions and roughly 100 API references, while repository testing is primarily Python/script based. Recent UI fixes involved settings ownership, overlapping session polling, missing documents, and bounded Gameyfin polling. A browser sweep is needed to expose integration and state bugs that source assertions cannot prove.

## 4. Problem statement

API tests can prove server contracts but cannot prove that real controls call them correctly, render success/failure states, preserve dialog state, handle rapid actions, or remain usable through keyboard, responsive, console, and network conditions.

## 5. Purpose / callers / contracts

- **Purpose:** exercise prioritized end-to-end journeys from visible control through API response and rendered state.
- **Callers:** mouse, keyboard, gamepad-oriented Big Box navigation, and browser automation.
- **Contracts:** controls are reachable and labeled; one action produces one request; errors are visible and recoverable; state refreshes without stale overwrite; settings collectors remain separated; dialogs reset correctly; polling terminates.

## 6. Preconditions

- e01s01 safety snapshot is complete.
- e01s02 isolated fixture/startup path is available.
- Server runs against temporary HOME/XDG data with synthetic game/library/media fixtures.
- Browser diagnostics start with a clean console/network buffer.
- `openbox.py` remains stopped.

## 7. Dependencies

- `[OK]` existing `web_app.py` and browser client.
- `[OK]` existing agent browser tooling for semantic interaction, diagnostics, and screenshots.
- No UI framework, test framework, or application package is added.

## 8. Requirements

### ADDED: Critical journeys have repeatable browser scripts

Each journey specifies setup, user actions, expected visible state, expected network behavior, console expectation, cleanup, and evidence path.

### ADDED: Fast-repeat and failure recovery are exercised

For mutation and polling controls, repeat/rapid actions and forced failures must not duplicate work, leave permanent loading states, or allow stale responses to overwrite newer state.

### ADDED: Accessibility and responsive smoke checks accompany functional checks

Keyboard access, focus return, visible labels, reduced-motion behavior, and narrow/wide layouts are checked on representative dialogs and Big Box navigation.

## 9. Invariants

- Use synthetic library/media and safe `echo`-style launcher fixtures only.
- Do not enter real credentials or connect destructive external accounts.
- Do not use the maintainer's default browser profile if it exposes unrelated data.
- Every console error or failed request is classified; none is dismissed because the page appears usable.
- Existing theme behavior may vary visually, but controls and state contracts must remain functional.

## 10. Journey matrix

| Priority | Journey | Actions | Expected evidence |
|---|---|---|---|
| P0 | Startup/auth/library | open tokenized URL; refresh; invalid-token request | library or empty state renders; protected calls authorized; invalid token rejected |
| P0 | Settings isolation | change one general field; save; open Storefront settings; save one field | unrelated general/storefront values survive; secrets not rendered |
| P0 | Launch/session | launch safe fixture; rapidly click once more; open sessions; stop/refresh | one intended launch; bounded status; no overlapping stale poll |
| P1 | Import/library mutation | import temporary folder; repeat import; edit/favorite/delete fixture | dedupe; correct card/detail state; no unrelated loss |
| P1 | Saves/backups | discover temp saves; backup; restore; missing-backup failure | contained paths; explicit terminal result; source survives failure |
| P1 | Updates/offline | check valid local fixture; force malformed/offline response | visible error; no browser NetworkError caused by dropped connection |
| P1 | Storefront/Gameyfin | load fixture catalog; install-status success/error/timeout | bounded polling; terminal UI; no forever spinner |
| P1 | Plugins/integrations | open status; run safe fixture; missing dependency/credential | actionable sanitized state; no secret exposure |
| P2 | Themes/Big Box | switch stock themes; enter/exit Big Box; keyboard navigation | controls visible; focus usable; no uncaught error |
| P2 | Responsive/accessibility | wide and narrow viewport; keyboard dialogs; reduced motion | no blocked critical control; focus returns; motion preference respected |

## 11. Out of scope

- Pixel-perfect visual signoff across every stock theme.
- Real purchases, donations, account creation, or third-party service mutation.
- Real ROM/game execution.
- Exhaustive gamepad hardware compatibility.
- Adding a permanent browser test framework during this sweep.

## 12. Detailed implementation steps

1. Start `web_app.py` with temporary HOME/XDG roots, seed empty and populated fixture states, capture the tokenized local URL, and record cleanup ownership → verify: `test -f specs/verifications/e01s03-browser-sweep.md && grep -q '^## Isolated browser fixture' specs/verifications/e01s03-browser-sweep.md`
2. Exercise startup/auth/library refresh and settings/storefront isolation at wide viewport; capture before/after values, request counts, console diagnostics, and screenshots → verify: `test -s specs/verifications/artifacts/e01s03-settings.png && grep -q '^### Settings isolation' specs/verifications/e01s03-browser-sweep.md`
3. Exercise safe launch/session, rapid-repeat protection, import/dedupe, edit/favorite/delete, and dialog reopen/reset behavior → verify: `test -s specs/verifications/artifacts/e01s03-launch-session.png && grep -q '^### Launch and session lifecycle' specs/verifications/e01s03-browser-sweep.md`
4. Exercise saves/backups, update failure, Storefront/Gameyfin terminal states, plugin/integration offline behavior, and recovery after each forced failure → verify: `test -s specs/verifications/artifacts/e01s03-failure-recovery.png && grep -q '^### Failure recovery' specs/verifications/e01s03-browser-sweep.md`
5. Smoke stock theme switching, Big Box enter/exit, keyboard-only dialog flow, narrow viewport, and reduced-motion preference; record blocked controls, focus loss, overflow, and console/network anomalies → verify: `test -s specs/verifications/artifacts/e01s03-responsive.png && grep -q '^### Accessibility and responsive smoke' specs/verifications/e01s03-browser-sweep.md`
6. Re-snapshot after every navigation or rerender, classify all console/page/network failures, and attach each credible anomaly to the candidate ledger with exact reproduction steps → verify: `! grep -Eq 'status: (new|untriaged)' specs/verifications/e01s03-browser-sweep.md`
7. Shut down the server, prove temporary data cleanup, and compare Git status with the safety snapshot → verify: `grep -q '^## Cleanup proof' specs/verifications/e01s03-browser-sweep.md && ! grep -q '\[ \]' specs/verifications/e01s03-browser-sweep.md`

## 13. Verification commands

```bash
test -f specs/verifications/e01s03-browser-sweep.md
test -s specs/verifications/artifacts/e01s03-settings.png
test -s specs/verifications/artifacts/e01s03-launch-session.png
test -s specs/verifications/artifacts/e01s03-failure-recovery.png
test -s specs/verifications/artifacts/e01s03-responsive.png
! grep -Eq 'status: (new|untriaged)' specs/verifications/e01s03-browser-sweep.md
! grep -q '\[ \]' specs/verifications/e01s03-browser-sweep.md
```

Browser execution follows: open tokenized URL → interactive snapshot → semantic click/fill using current references → re-snapshot after navigation/rerender → diagnostics → screenshot. Forms that mutate state are completed only against fixtures.

## 14. Evidence format

`specs/verifications/e01s03-browser-sweep.md` contains fixture paths, viewport, journey checklist, action sequence, expected/actual visible text, request count/status, console/page errors, screenshot path, cleanup, candidate ID, and disposition. Screenshots live under `specs/verifications/artifacts/`.

## 15. Failure handling

- Capture screenshot, current URL, visible state, console/page errors, failed request details, and fixture state before retrying.
- Retry once from a clean reload; if timing-related, repeat ten times and record failure rate.
- If a mutation may have duplicated, stop and inspect fixture state before further clicks.
- If browser references become stale after rerender, re-snapshot rather than guessing selectors.

## 16. Risks and mitigations

- **Hidden real profile/data:** isolated browser profile and temporary OpenBox roots.
- **False manual pass:** require visible, network, console, and persisted-state evidence.
- **Duplicate mutation:** count network requests and inspect state after rapid actions.
- **Theme-specific noise:** confirm base behavior in the default theme before classifying stock-theme variants.
- **Time overrun:** execute P0 rows first, then P1; P2 is bounded smoke only.

## 17. Acceptance criteria

- [ ] Every P0 and P1 journey has completed or externally blocked evidence.
- [ ] Screenshots exist for settings, launch/session, failure recovery, and responsive states.
- [ ] Console, page, and network diagnostics were inspected for each journey group.
- [ ] Rapid-repeat and failure-recovery cases were exercised.
- [ ] Every anomaly has a terminal current disposition and candidate link.
- [ ] Server stopped and temporary data cleanup is proven.

## 18. Verification script (step-by-step)

1. Open the isolated tokenized URL and verify the expected fixture library.
2. Complete each journey row in priority order, checking visible state, network request, console, and persisted fixture state.
3. After any rerender, capture a fresh accessibility snapshot before the next action.
4. Capture the four required screenshots and link them from the report.
5. Re-run one successful and one failed journey after a clean reload to prove recovery.
6. Stop the server, remove temporary roots, and compare Git status to the initial safety snapshot.

## 19. Stop conditions

Stop mutations if the server points at real user data, a real credential appears, an action triggers a real purchase/post/account change, a duplicate launch cannot be contained, or a data-loss candidate is observed. Preserve state and prioritize root cause immediately.

## 20. Handoff

Gate: P0/P1 journeys and cleanup complete; every anomaly dispositioned. Next: e01s04 receives confirmed browser defects with screenshots, request evidence, and deterministic steps.
