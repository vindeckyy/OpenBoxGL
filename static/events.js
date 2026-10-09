/* The tab's one server-sent event stream (/api/events).

   Every module that wants push updates subscribes by event kind here instead of
   opening its own EventSource. Each tab used to hold up to three, and the server
   caps subscribers, so a busy tab got 503 (SSE_BUSY) and lost updates.

   - One EventSource per tab, opened on the first subscription.
   - A dropped stream is retried with backoff (pure.js::nextRetryDelay).
   - state.js closes registered streams when the page is hidden; the stream
     reopens when the page is visible again, and onOpen handlers run on every
     successful open, so a consumer can catch up on what it missed.
*/
import { token, registerLifecycleStream, unregisterLifecycleStream } from './state.js';
import { nextRetryDelay } from './pure.js';

const RETRY_MIN_MS = 3000;
const RETRY_MAX_MS = 60000;
const EVENT_SOURCE_CLOSED = 2;

const kindHandlers = new Map();   // event kind -> Set of handlers
const openHandlers = new Set();   // run on every successful (re)connect
const attached = new Set();       // kinds with a listener on the live source
let source = null;
let retryTimer = 0;
let retryDelay = RETRY_MIN_MS;

function hasSubscribers() {
  return kindHandlers.size > 0 || openHandlers.size > 0;
}

function attach(kind) {
  if (!source || attached.has(kind)) return;
  attached.add(kind);
  source.addEventListener(kind, event => {
    for (const handler of kindHandlers.get(kind) || []) {
      try { handler(event); } catch (error) { console.error(`event handler for ${kind} failed`, error); }
    }
  });
}

function drop() {
  if (!source) return;
  const dying = source;
  source = null;
  attached.clear();
  unregisterLifecycleStream(dying);
  try { dying.close(); } catch { /* already closed */ }
}

function scheduleRetry() {
  if (retryTimer || !hasSubscribers()) return;
  retryTimer = setTimeout(() => {
    retryTimer = 0;
    if (!document.hidden) open();
  }, retryDelay);
  retryDelay = nextRetryDelay(retryDelay, RETRY_MAX_MS);
}

function open() {
  clearTimeout(retryTimer);
  retryTimer = 0;
  if (source || !hasSubscribers()) return;
  let next;
  try {
    next = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
  } catch {
    scheduleRetry();
    return;
  }
  source = next;
  attached.clear();
  registerLifecycleStream(next);
  next.onopen = () => {
    retryDelay = RETRY_MIN_MS;
    for (const handler of openHandlers) {
      try { handler(); } catch (error) { console.error('event open handler failed', error); }
    }
  };
  next.onerror = () => {
    drop();
    scheduleRetry();
  };
  for (const kind of kindHandlers.keys()) attach(kind);
}

// A stream closed by the lifecycle (page hidden) keeps its object but reads CLOSED.
function resume() {
  if (document.hidden || !hasSubscribers()) return;
  if (!source || source.readyState === EVENT_SOURCE_CLOSED) {
    drop();
    open();
  }
}

document.addEventListener('visibilitychange', resume);
window.addEventListener('pageshow', resume);

/** Subscribe to one or more event kinds. Returns an unsubscribe function. */
export function subscribe(kinds, handler) {
  const list = Array.isArray(kinds) ? kinds : [kinds];
  for (const kind of list) {
    if (!kindHandlers.has(kind)) kindHandlers.set(kind, new Set());
    kindHandlers.get(kind).add(handler);
    attach(kind);
  }
  open();
  return () => {
    for (const kind of list) kindHandlers.get(kind)?.delete(handler);
  };
}

/** Run handler on every successful connect, including the first. Returns an unsubscribe function. */
export function onOpen(handler) {
  openHandlers.add(handler);
  open();
  return () => openHandlers.delete(handler);
}
