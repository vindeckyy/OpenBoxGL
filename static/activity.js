import { $, escapeHtml } from './util.js';
import { api, notify, token } from './state.js';

const JOB_SSE_EVENTS = ['job.queued', 'job.progress', 'job.cancelling', 'job.finished', 'job.interrupted'];
const ACTIVE_STATES = new Set(['queued', 'running', 'cancelling']);
const ATTENTION_STATES = new Set(['partial', 'error', 'interrupted']);
const RECENT_MS = 30 * 24 * 60 * 60 * 1000;
const ROW_KEYS = ['job_id', 'type', 'title', 'state', 'phase', 'current', 'total', 'message', 'can_cancel', 'can_retry', 'can_resume'];

/** @type {Map<string, Record<string, unknown>>} */
const jobsById = new Map();
/** @type {Map<string, { items: object[], next_cursor: string|null }>} */
const itemPages = new Map();
/** @type {Set<string>} */
const expandedJobs = new Set();
let sseSource = null;
let sseReconnectTimer = null;
let initialized = false;

function blankOperation(jobId) {
  return {
    job_id: jobId,
    root_job_id: jobId,
    retry_of: null,
    resume_of: null,
    type: '',
    title: '',
    state: 'queued',
    phase: '',
    current: 0,
    total: 0,
    message: '',
    created_at: null,
    updated_at: null,
    started_at: null,
    finished_at: null,
    can_cancel: false,
    can_retry: false,
    can_resume: false,
    input: {},
    checkpoint: null,
    result: null,
    error: null,
  };
}

function needsAttention(job) {
  return ATTENTION_STATES.has(job.state) && (job.can_retry || job.can_resume);
}

function isActive(job) {
  return ACTIVE_STATES.has(job.state);
}

function finishedWithinRecentWindow(job) {
  if (!job.finished_at) return true;
  return Date.now() - new Date(job.finished_at).getTime() <= RECENT_MS;
}

function isRecentCandidate(job) {
  if (job.state === 'done' || job.state === 'cancelled') return finishedWithinRecentWindow(job);
  if ((job.state === 'error' || job.state === 'partial') && !job.can_retry && !job.can_resume) {
    return finishedWithinRecentWindow(job);
  }
  return false;
}

export function partitionJobs(jobs) {
  const active = [];
  const attention = [];
  const recent = [];
  const excluded = new Set();
  for (const job of jobs) {
    if (isActive(job)) {
      active.push(job);
      excluded.add(job.job_id);
    }
  }
  for (const job of jobs) {
    if (!excluded.has(job.job_id) && needsAttention(job)) {
      attention.push(job);
      excluded.add(job.job_id);
    }
  }
  for (const job of jobs) {
    if (!excluded.has(job.job_id) && isRecentCandidate(job)) recent.push(job);
  }
  return {active, attention, recent};
}

function formatElapsed(job) {
  const start = job.started_at || job.created_at;
  if (!start) return '';
  const endMs = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((endMs - new Date(start).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function stateBadgeClass(state) {
  if (state === 'partial') return 'activity-badge-partial';
  if (state === 'done') return 'activity-badge-done';
  if (state === 'error' || state === 'interrupted') return 'activity-badge-error';
  if (state === 'cancelled') return 'activity-badge-cancelled';
  return 'activity-badge-active';
}

function stateBadgeLabel(state) {
  if (state === 'done') return 'Done ✓';
  if (state === 'partial') return 'Partial';
  return String(state || 'unknown');
}

function ensureActivityDrawer() {
  if ($('activityDrawer')) return;
  const dialog = document.createElement('dialog');
  dialog.id = 'activityDrawer';
  dialog.className = 'activity-drawer-dialog';
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'activityDrawerTitle');
  dialog.innerHTML = `
    <div class="dialog-head activity-drawer-head">
      <h2 id="activityDrawerTitle">Activity</h2>
      <button type="button" id="closeActivityDrawer" aria-label="Close Activity drawer">×</button>
    </div>
    <div class="activity-drawer-body" id="activityDrawerBody" role="region" aria-label="Background operations">
      <section class="activity-section" data-activity-section="active">
        <h3 class="activity-section-title">Active</h3>
        <div class="activity-list" id="activityActiveList"></div>
      </section>
      <section class="activity-section" data-activity-section="attention">
        <h3 class="activity-section-title">Needs attention</h3>
        <div class="activity-list" id="activityAttentionList"></div>
      </section>
      <section class="activity-section" data-activity-section="recent">
        <h3 class="activity-section-title">Recent</h3>
        <div class="activity-list" id="activityRecentList"></div>
      </section>
    </div>
  `;
  document.body.appendChild(dialog);
  $('closeActivityDrawer').onclick = () => dialog.close();
}

function renderEmpty(sectionEl, message) {
  sectionEl.innerHTML = `<p class="description activity-empty">${escapeHtml(message)}</p>`;
}

function renderJobRow(job) {
  const progress = Number(job.total) > 0 ? `${job.current}/${job.total}` : '';
  const errorText = job.error?.message ? String(job.error.message) : '';
  const expanded = expandedJobs.has(job.job_id);
  const itemState = itemPages.get(job.job_id);
  const itemsHtml = expanded && itemState
    ? `<div class="activity-items" data-activity-items="${escapeHtml(job.job_id)}">
        ${(itemState.items || []).map(item => `<div class="activity-item" data-item-id="${escapeHtml(item.item_id)}" data-item-state="${escapeHtml(item.state)}">
          <strong>${escapeHtml(item.label)}</strong>
          <span class="activity-item-state">${escapeHtml(item.state)}</span>
          ${item.error ? `<small class="activity-item-error">${escapeHtml(item.error.message || item.error.code || '')}</small>` : ''}
        </div>`).join('')}
        ${itemState.next_cursor ? `<button type="button" class="icon-button activity-items-more" data-activity-items-more="${escapeHtml(job.job_id)}">Load more failures</button>` : ''}
      </div>`
    : '';
  const attrs = ROW_KEYS.map(key => `data-${key.replace(/_/g, '-')}="${escapeHtml(String(job[key] ?? ''))}"`).join(' ');
  return `<article class="activity-row" ${attrs}>
    <div class="activity-row-head">
      <div class="activity-row-title">
        <strong>${escapeHtml(job.title || job.type || job.job_id)}</strong>
        <span class="activity-row-type">${escapeHtml(job.type || '')}</span>
      </div>
      <span class="activity-badge ${stateBadgeClass(job.state)}" data-activity-state-badge="${escapeHtml(job.state)}">${escapeHtml(stateBadgeLabel(job.state))}</span>
    </div>
    <div class="activity-row-meta">
      <span class="activity-row-phase">${escapeHtml(job.phase || '')}</span>
      ${progress ? `<span class="activity-row-progress">${escapeHtml(progress)}</span>` : ''}
      <span class="activity-row-elapsed">${escapeHtml(formatElapsed(job))}</span>
    </div>
    ${job.message ? `<p class="activity-row-message">${escapeHtml(job.message)}</p>` : ''}
    ${errorText ? `<p class="activity-row-error">${escapeHtml(errorText)}</p>` : ''}
    <div class="activity-row-actions">
      <button type="button" class="icon-button" data-activity-cancel="${escapeHtml(job.job_id)}" ${job.can_cancel ? '' : 'disabled'}>Cancel</button>
      <button type="button" class="icon-button" data-activity-retry="${escapeHtml(job.job_id)}" ${job.can_retry ? '' : 'hidden'}>Retry</button>
      <button type="button" class="icon-button" data-activity-resume="${escapeHtml(job.job_id)}" ${job.can_resume ? '' : 'hidden'}>Resume</button>
      <button type="button" class="icon-button" data-activity-toggle-items="${escapeHtml(job.job_id)}">${expanded ? 'Hide failures' : 'Show failures'}</button>
    </div>
    ${itemsHtml}
  </article>`;
}

function renderActivity() {
  ensureActivityDrawer();
  const jobs = [...jobsById.values()];
  const {active, attention, recent} = partitionJobs(jobs);
  const sections = [
    ['activityActiveList', active, 'No active operations.'],
    ['activityAttentionList', attention, 'Nothing needs attention.'],
    ['activityRecentList', recent, 'No recent operations in the last 30 days.'],
  ];
  for (const [id, list, emptyMessage] of sections) {
    const el = $(id);
    if (!el) continue;
    if (!list.length) renderEmpty(el, emptyMessage);
    else el.innerHTML = list.map(renderJobRow).join('');
  }
  bindActivityRowActions();
}

function bindActivityRowActions() {
  document.querySelectorAll('[data-activity-cancel]').forEach(button => {
    button.onclick = () => cancelJob(button.dataset.activityCancel, button);
  });
  document.querySelectorAll('[data-activity-retry]').forEach(button => {
    button.onclick = () => retryJob(button.dataset.activityRetry);
  });
  document.querySelectorAll('[data-activity-resume]').forEach(button => {
    button.onclick = () => resumeJob(button.dataset.activityResume);
  });
  document.querySelectorAll('[data-activity-toggle-items]').forEach(button => {
    button.onclick = () => toggleJobItems(button.dataset.activityToggleItems);
  });
  document.querySelectorAll('[data-activity-items-more]').forEach(button => {
    button.onclick = () => loadJobItems(button.dataset.activityItemsMore, {append: true});
  });
}

async function fetchAllJobs() {
  let cursor = null;
  const jobs = [];
  do {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}&limit=100` : '?limit=100';
    const page = await api(`/api/v2/jobs${query}`);
    jobs.push(...(page.jobs || []));
    cursor = page.next_cursor || null;
  } while (cursor);
  return jobs;
}

function replaceJobs(jobs) {
  jobsById.clear();
  for (const job of jobs) jobsById.set(job.job_id, job);
  updateActivityCount();
}

function updateActivityCount() {
  const countEl = $('activityCount');
  if (!countEl) return;
  const {active, attention} = partitionJobs([...jobsById.values()]);
  const count = active.length + attention.length;
  countEl.textContent = String(count);
  countEl.hidden = count === 0;
}

function mergeJobPatch(jobId, patch) {
  const existing = jobsById.get(jobId) || blankOperation(jobId);
  jobsById.set(jobId, {...existing, ...patch, job_id: jobId});
  updateActivityCount();
  if ($('activityDrawer')?.open) renderActivity();
}

function onJobSse(event) {
  let payload;
  try { payload = JSON.parse(event.data); } catch { return; }
  if (!payload?.job_id) return;
  mergeJobPatch(payload.job_id, payload);
}

function connectActivitySse() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (sseSource) {
    try { sseSource.close(); } catch { /* already closed */ }
    sseSource = null;
  }
  try {
    sseSource = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
    for (const kind of JOB_SSE_EVENTS) sseSource.addEventListener(kind, onJobSse);
    sseSource.onerror = () => {
      if (sseSource) {
        try { sseSource.close(); } catch { /* noop */ }
        sseSource = null;
      }
      sseReconnectTimer = setTimeout(() => {
        connectActivitySse();
        refreshJobsSnapshot().catch(() => {});
      }, 3000);
    };
  } catch {
    sseReconnectTimer = setTimeout(() => connectActivitySse(), 5000);
  }
}

async function refreshJobsSnapshot() {
  const jobs = await fetchAllJobs();
  replaceJobs(jobs);
  if ($('activityDrawer')?.open) renderActivity();
}

async function cancelJob(jobId, button) {
  if (button?.disabled) return;
  try {
    await api('/api/v2/jobs/cancel', {method: 'POST', body: JSON.stringify({job_id: jobId})});
    notify('Cancellation requested');
    await refreshJobsSnapshot();
  } catch (error) {
    notify(error.message || 'Could not cancel job');
  }
}

async function retryJob(jobId) {
  try {
    await api('/api/v2/jobs/retry', {method: 'POST', body: JSON.stringify({job_id: jobId})});
    notify('Retry queued');
    await refreshJobsSnapshot();
  } catch (error) {
    notify(error.message || 'Could not retry job');
  }
}

async function resumeJob(jobId) {
  try {
    await api('/api/v2/jobs/resume', {method: 'POST', body: JSON.stringify({job_id: jobId})});
    notify('Resume queued');
    await refreshJobsSnapshot();
  } catch (error) {
    notify(error.message || 'Could not resume job');
  }
}

async function loadJobItems(jobId, {append = false} = {}) {
  const existing = itemPages.get(jobId) || {items: [], next_cursor: null};
  const cursor = append ? existing.next_cursor : null;
  const query = new URLSearchParams({job_id: jobId, limit: '50'});
  if (cursor) query.set('cursor', cursor);
  const page = await api(`/api/v2/jobs/items?${query.toString()}`);
  const items = append ? [...existing.items, ...(page.items || [])] : (page.items || []);
  itemPages.set(jobId, {items, next_cursor: page.next_cursor || null});
  renderActivity();
}

async function toggleJobItems(jobId) {
  if (expandedJobs.has(jobId)) {
    expandedJobs.delete(jobId);
    renderActivity();
    return;
  }
  expandedJobs.add(jobId);
  if (!itemPages.has(jobId)) await loadJobItems(jobId);
  else renderActivity();
}

export async function openActivity() {
  ensureActivityDrawer();
  await refreshJobsSnapshot();
  $('activityDrawer').showModal();
  renderActivity();
}

async function initActivity() {
  if (initialized) return;
  initialized = true;
  ensureActivityDrawer();
  await refreshJobsSnapshot();
  connectActivitySse();
  if ($('activityButton')) $('activityButton').onclick = () => openActivity();
}

queueMicrotask(() => { initActivity().catch(() => {}); });
