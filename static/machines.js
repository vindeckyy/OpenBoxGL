// Shared state literals for the job lifecycles the UI polls.
// Mirrors pkg/state/machines.py; keep the literals identical.

export const JOB_ACTIVE = new Set(['queued', 'running', 'cancelling']);
export const JOB_TERMINAL = new Set(['done', 'partial', 'error', 'cancelled', 'interrupted']);
export const JOB_ATTENTION = new Set(['partial', 'error', 'interrupted']);

// Legacy facade jobs (INSTALLS / METADATA_JOB / MATCH_JOB / MEDIA_JOB).
export const FACADE_ACTIVE = new Set(['installing', 'updating', 'downloading', 'running']);

// Storefront sweep per source.
export const STOREFRONT_PENDING = new Set(['pending', 'running']);

export const isJobActive = job => JOB_ACTIVE.has(job?.state);
export const isJobTerminal = job => JOB_TERMINAL.has(job?.state);
export const isJobAttention = job => JOB_ATTENTION.has(job?.state);
