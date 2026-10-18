# ADR 0076: Translated server notices

**Date:** 2026-10-18
**Status:** Accepted (1.17.0)

## Context

The 1.16.1 known limits said the client's notification text was translated while server notices (cloud sync,
launch-check changes, rejected definition packs, webhook failures) were English. The notification feed persists the
English strings, so a non-English user saw mixed languages and old notifications could not be retranslated.

## Decision

- A notification item may carry `title_key`, `body_key`, and a small `params` object in addition to the stored English
  `title`/`body`.
- `notifications.SERVER_NOTICE_KEYS` is the single list of server keys and their required placeholders.
- The notification center translates a keyed item with the active locale and falls back to the stored English text
  when a key is missing. Items saved before this change carry no keys and render exactly as before.
- Parameters are bounded (10 keys, 200 characters per string) so the persisted notification stays small.
- The English text is still written on every notice for API consumers and webhook payloads.

## Evidence

`tests/test_notifications.py` checks key/parameter persistence, truncation, old payloads, every server key in all five
locales, placeholder parity, and that every `whatsnew.js` highlight has locale keys. Producer tests in
`tests/test_cloud_sync.py`, `tests/test_sse.py`, and `tests/test_launch_audit.py` continue to pass.

## Consequences

- Only notification titles and bodies are translated. Other server responses and remaining English dialog labels are
  a separate change.
- A renamed or removed locale key will fail the notification test before it reaches a release.
