# ADR 0068: One event stream per tab

**Date:** 2026-10-08
**Status:** Accepted (1.16.1)

## Context

Three frontend modules each opened their own `EventSource` to `/api/events`: the activity
drawer, the session cards and the health rescan toast. The server caps event subscribers at
16 (`SSE_MAX_SUBSCRIBERS`), and a page navigation leaves the previous page's subscribers
open until the server's heartbeat notices (up to 15 seconds). The browser smoke suite opened
enough pages in a few seconds to hit the cap: `/api/events` answered 503 (`SSE_BUSY`) and the
updates behind it stopped. The first reconnect fix made it worse, because each page load
then opened a second session stream.

## Decision

- `static/events.js` is the only place that constructs an `EventSource`. It owns the
  stream, retries a drop with backoff (`pure.js::nextRetryDelay`), registers the stream with
  the page-lifecycle close (`state.js`), and reopens it when the page is visible again.
- Consumers subscribe by event kind (`subscribe`) and catch up on every successful connect
  (`onOpen`). The session module polls once on connect, the activity module refreshes its
  snapshot.
- The contract test `tests/test_frontend_contract.py::test_one_event_stream_per_tab_owned_by_events_js`
  fails if a second `EventSource` is constructed anywhere in `static/`.

## Consequences

- A tab holds one subscriber, so the cap is reached by three or more open tabs, not by
  page loads.
- A consumer that needs state from before its subscription must catch up through `onOpen`;
  a new consumer that forgets to is a bug the test does not catch. Review should look for it.
- The browser behaviour of the reopen-on-return path is covered by the manual check in
  `docs/reliability.md` row 60 (the harness cannot toggle page visibility).
