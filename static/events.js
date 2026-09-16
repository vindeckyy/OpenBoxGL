/* events.js — the single shared /api/events EventSource (P1-3/P1-4).

   Before 1.13.0 sessions.js and activity.js each constructed their own
   EventSource. Sessions closed its stream on the first error and never
   reconnected, while activity reconnected on a fixed timer; the server saw two
   streams per page. This module owns the only EventSource construction site,
   fans named SSE frames out to subscribers, reconnects with capped backoff,
   and closes on unload.

   Usage:
     const unsubscribe = onServerEvent('job.progress', event => { ... });
     unsubscribe(); // stop listening
*/
import { token } from './state.js';

const NAMED_EVENTS = [
  'session.started', 'session.stopped', 'session.state',
  'job.queued', 'job.progress', 'job.cancelling', 'job.finished', 'job.interrupted',
  'state.changed', 'session.recap',
];
const BASE_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30000;

let source = null;
let reconnectTimer = 0;
let backoffMs = BASE_BACKOFF_MS;
let connectedOnce = false;
let stopped = false;
/** @type {Set<{kind: string, handler: (event: MessageEvent | null) => void}>} */
const subscribers = new Set();

function dispatch(kind, event) {
  for (const subscriber of [...subscribers]) {
    if (subscriber.kind !== kind) continue;
    try {
      subscriber.handler(event);
    } catch (error) {
      console.error('events: subscriber failed', error);
    }
  }
}

function scheduleReconnect() {
  if (stopped || reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = 0;
    openStream();
  }, backoffMs);
  backoffMs = Math.min(MAX_BACKOFF_MS, backoffMs * 2);
}

function openStream() {
  if (stopped || source) return;
  let next;
  try {
    next = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
  } catch {
    // EventSource unsupported; polling fallbacks stay in charge.
    scheduleReconnect();
    return;
  }
  source = next;
  for (const kind of NAMED_EVENTS) next.addEventListener(kind, event => dispatch(kind, event));
  next.onmessage = event => dispatch('message', event);
  next.onopen = () => {
    backoffMs = BASE_BACKOFF_MS;
    if (connectedOnce) dispatch('reconnected', null);
    connectedOnce = true;
  };
  next.onerror = () => {
    if (source === next) {
      try { next.close(); } catch { /* already closed */ }
      source = null;
    }
    scheduleReconnect();
  };
}

/**
 * Subscribe to a named SSE frame (or 'message' for unnamed frames, or
 * 'reconnected' after a reconnect). Opens the shared stream on first use.
 * @param {string} kind
 * @param {(event: MessageEvent | null) => void} handler
 * @returns {() => void} unsubscribe
 */
export function onServerEvent(kind, handler) {
  const subscriber = { kind, handler };
  subscribers.add(subscriber);
  stopped = false;
  openStream();
  return () => { subscribers.delete(subscriber); };
}

const TERMINAL_JOB_STATES = ['done', 'partial', 'error', 'cancelled', 'interrupted'];

/**
 * Resolve when SSE reports the terminal event for one job.
 *
 * ``onProgress(job)`` fires per ``job.progress`` frame carrying
 * ``{job_id, state, phase, current, total, message}``; the resolved object
 * from ``job.finished``/``job.interrupted`` adds ``result``/``error``.
 * ``fetch()`` is an optional one-shot re-read used after an SSE reconnect,
 * in case the terminal frame fired while the stream was down.
 */
export function waitForJob(jobId, { onProgress, fetch } = {}) {
  return new Promise(resolve => {
    let done = false;
    const offs = [];
    const finish = job => {
      if (done) return;
      done = true;
      offs.forEach(off => off());
      resolve(job);
    };
    const parse = event => {
      try { return JSON.parse(event.data); } catch { return null; }
    };
    offs.push(
      onServerEvent('job.progress', event => {
        const job = parse(event);
        if (job?.job_id === jobId) onProgress?.(job);
      }),
      onServerEvent('job.finished', event => {
        const job = parse(event);
        if (job?.job_id === jobId) finish(job);
      }),
      onServerEvent('job.interrupted', event => {
        const job = parse(event);
        if (job?.job_id === jobId) finish(job);
      }),
    );
    if (fetch) {
      // One fetch upfront covers a job that finished before we subscribed;
      // the same check on 'reconnected' covers frames missed while down.
      const check = () => fetch().then(job => {
        if (job && TERMINAL_JOB_STATES.includes(job.state)) finish(job);
      }).catch(() => {});
      check();
      offs.push(onServerEvent('reconnected', check));
    }
  });
}

/** Close the shared stream and cancel any pending reconnect. */
export function closeEventStream() {
  stopped = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = 0;
  }
  if (source) {
    try { source.close(); } catch { /* already closed */ }
    source = null;
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', closeEventStream);
  window.addEventListener('pagehide', closeEventStream);
}

export { NAMED_EVENTS };
