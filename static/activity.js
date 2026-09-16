import { $, escapeHtml } from './util.js';
import { api, notify, notifyError } from './state.js';
import { onServerEvent } from './events.js';
import { openDialog, closeDialog } from './dialogs.js';
import { t, onLocaleChange } from './i18n.js';

const JOB_SSE_EVENTS = ['job.queued', 'job.progress', 'job.cancelling', 'job.finished', 'job.interrupted'];
const ACTIVE_STATES = new Set(['queued', 'running', 'cancelling']);
const ATTENTION_STATES = new Set(['partial', 'error', 'interrupted']);
const RECENT_MS = 30 * 24 * 60 * 60 * 1000;
const ROW_KEYS = ['job_id', 'type', 'title', 'state', 'phase', 'current', 'total', 'message', 'can_cancel', 'can_retry', 'can_resume'];
const RESULT_KEYS = { added: 'activity.result_added', merged: 'activity.result_merged', skipped: 'activity.result_skipped', excluded: 'activity.result_excluded', completed: 'activity.result_completed', downloaded: 'activity.result_downloaded' };
const TERMINAL_STATES = new Set(['done', 'error', 'partial', 'interrupted', 'cancelled']);

/** @type {Map<string, Record<string, unknown>>} */
const jobsById = new Map();
/** @type {Map<string, { items: object[], next_cursor: string|null }>} */
const itemPages = new Map();
/** @type {Set<string>} */
const expandedJobs = new Set();
let jobEventsBound = false;
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

function groupJobsByRoot(jobs) {
  const grouped = new Map();
  for (const job of jobs) {
    const root = job.root_job_id || job.job_id;
    if (!grouped.has(root)) grouped.set(root, []);
    grouped.get(root).push(job);
  }
  for (const [root, list] of grouped) {
    list.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
  }
  return grouped;
}

function filterJobs(jobs, {typeFilter = null, stateFilter = null} = {}) {
  let result = jobs;
  if (typeFilter) result = result.filter(job => job.type === typeFilter);
  if (stateFilter) result = result.filter(job => job.state === stateFilter);
  return result;
}

function stateLabel(state) {
  const key = `activity.state_${state}`;
  const value = t(key);
  return value === key ? String(state || t('activity.state_unknown')) : value;
}

function jobSummaryLine(job) {
  const state = job.state;
  const result = job.result || {};
  const error = job.error || {};
  if (state === 'done' && result && Object.keys(result).length) {
    const parts = [];
    for (const key of Object.keys(RESULT_KEYS)) {
      if (key in result) parts.push(t(RESULT_KEYS[key], {count: result[key]}));
    }
    if (parts.length) return t('activity.finished_summary', {parts: parts.join(', ')});
    return t('activity.finished_success');
  }
  if (['error', 'partial', 'interrupted'].includes(state) && error && (error.message || error.code)) {
    return t('activity.finished_with', {state: stateLabel(state), message: error.message || error.code || ''});
  }
  if (state === 'cancelled') return t('activity.state_cancelled');
  if (state) return t('activity.finished_state', {state: stateLabel(state)});
  return t('activity.finished');
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
  if (state === 'done') return t('activity.badge_done');
  if (state === 'partial') return t('activity.state_partial');
  return stateLabel(state);
}

let activityTypeFilter = '';
let activityStateFilter = '';

// P7 live region: announce job state transitions (not every progress tick) in
// a debounced batch so #activityAnnouncer is useful without being noisy.
const announcedStates = new Map();
const pendingAnnouncements = [];
let announcerTimer = 0;

function announceJobState(job, previousState) {
  if (!job || job.state === previousState) return;
  if (!TERMINAL_STATES.has(job.state) && !ACTIVE_STATES.has(job.state)) return;
  const announced = announcedStates.get(job.job_id);
  if (announced === job.state) return;
  let key = null;
  if (job.state === 'running' && !announced) key = 'activity.announce_started';
  else if (job.state === 'done') key = 'activity.announce_done';
  else if (['error', 'partial', 'interrupted'].includes(job.state)) key = 'activity.announce_failed';
  if (!key) return;
  if (job.state === 'running' && $('activityDrawer')?.open) {
    announcedStates.set(job.job_id, job.state);
    return;
  }
  announcedStates.set(job.job_id, job.state);
  pendingAnnouncements.push(t(key, {title: job.title || job.type || job.job_id}));
  if (announcerTimer) return;
  announcerTimer = setTimeout(() => {
    announcerTimer = 0;
    const el = $('activityAnnouncer');
    if (el) el.textContent = pendingAnnouncements.splice(0).join('. ');
  }, 500);
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
      <h2 id="activityDrawerTitle">${escapeHtml(t('activity.title'))}</h2>
      <button type="button" id="closeActivityDrawer" aria-label="${escapeHtml(t('activity.close'))}">×</button>
    </div>
    <div class="activity-filters" id="activityFilters">
      <label class="field"><span id="activityTypeFilterLabel">${escapeHtml(t('activity.type'))}</span> <select id="activityFilterType"><option value="">${escapeHtml(t('activity.all_types'))}</option><option value="setup.scan">${escapeHtml(t('activity.type_setup_scan'))}</option><option value="setup.commit">${escapeHtml(t('activity.type_setup_commit'))}</option><option value="media.bulk_download">${escapeHtml(t('activity.type_media'))}</option><option value="metadata.match_preview">${escapeHtml(t('activity.type_metadata'))}</option></select></label>
      <label class="field"><span id="activityStateFilterLabel">${escapeHtml(t('activity.state'))}</span> <select id="activityFilterState"><option value="">${escapeHtml(t('activity.all_states'))}</option><option value="queued">${escapeHtml(stateLabel('queued'))}</option><option value="running">${escapeHtml(stateLabel('running'))}</option><option value="done">${escapeHtml(stateLabel('done'))}</option><option value="error">${escapeHtml(stateLabel('error'))}</option><option value="partial">${escapeHtml(stateLabel('partial'))}</option><option value="interrupted">${escapeHtml(stateLabel('interrupted'))}</option></select></label>
    </div>
    <div class="activity-drawer-body" id="activityDrawerBody" role="region" aria-label="${escapeHtml(t('activity.region_label'))}">
      <section class="activity-section" data-activity-section="active">
        <h3 class="activity-section-title" id="activityActiveTitle">${escapeHtml(t('activity.section_active'))}</h3>
        <div class="activity-list" id="activityActiveList"></div>
      </section>
      <section class="activity-section" data-activity-section="attention">
        <h3 class="activity-section-title" id="activityAttentionTitle">${escapeHtml(t('activity.section_attention'))}</h3>
        <div class="activity-list" id="activityAttentionList"></div>
      </section>
      <section class="activity-section" data-activity-section="recent">
        <h3 class="activity-section-title" id="activityRecentTitle">${escapeHtml(t('activity.section_recent'))}</h3>
        <div class="activity-list" id="activityRecentList"></div>
      </section>
      <section class="activity-section" data-activity-section="grouped" id="activityGroupedSection" hidden>
        <h3 class="activity-section-title" id="activityGroupedTitle">${escapeHtml(t('activity.section_grouped'))}</h3>
        <div class="activity-list" id="activityGroupedList"></div>
      </section>
    </div>
  `;
  document.body.appendChild(dialog);
  $('closeActivityDrawer').onclick = () => closeDialog(dialog);
  const typeSel = $('activityFilterType');
  const stateSel = $('activityFilterState');
  if (typeSel) typeSel.onchange = () => { activityTypeFilter = typeSel.value; renderActivity(); };
  if (stateSel) stateSel.onchange = () => { activityStateFilter = stateSel.value; renderActivity(); };
}

function localizeDrawer() {
  if (!$('activityDrawer')) return;
  $('activityDrawerTitle').textContent = t('activity.title');
  const close = $('closeActivityDrawer');
  if (close) close.setAttribute('aria-label', t('activity.close'));
  const typeLabel = $('activityTypeFilterLabel');
  if (typeLabel) typeLabel.textContent = t('activity.type');
  const stateFilterLabel = $('activityStateFilterLabel');
  if (stateFilterLabel) stateFilterLabel.textContent = t('activity.state');
  $('activityActiveTitle').textContent = t('activity.section_active');
  $('activityAttentionTitle').textContent = t('activity.section_attention');
  $('activityRecentTitle').textContent = t('activity.section_recent');
  $('activityGroupedTitle').textContent = t('activity.section_grouped');
  const body = $('activityDrawerBody');
  if (body) body.setAttribute('aria-label', t('activity.region_label'));
  const typeSel = $('activityFilterType');
  if (typeSel) {
    const values = ['', 'setup.scan', 'setup.commit', 'media.bulk_download', 'metadata.match_preview'];
    const keys = ['activity.all_types', 'activity.type_setup_scan', 'activity.type_setup_commit', 'activity.type_media', 'activity.type_metadata'];
    [...typeSel.options].forEach((option, index) => { if (keys[index]) option.textContent = t(keys[index]); });
    typeSel.value = values.includes(typeSel.value) ? typeSel.value : '';
  }
  const stateSel = $('activityFilterState');
  if (stateSel) {
    [...stateSel.options].forEach(option => {
      option.textContent = option.value ? stateLabel(option.value) : t('activity.all_states');
    });
  }
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
          <span class="activity-item-state">${escapeHtml(stateLabel(item.state))}</span>
          ${item.error ? `<small class="activity-item-error">${escapeHtml(item.error.message || item.error.code || '')}</small>` : ''}
        </div>`).join('')}
        ${itemState.next_cursor ? `<button type="button" class="icon-button activity-items-more" data-activity-items-more="${escapeHtml(job.job_id)}">${escapeHtml(t('activity.load_more_failures'))}</button>` : ''}
      </div>`
    : '';
  const summary = jobSummaryLine(job);
  const showSummary = TERMINAL_STATES.has(job.state);
  const summaryHtml = showSummary ? `<p class="activity-row-summary" data-activity-summary="${escapeHtml(job.job_id)}">${escapeHtml(summary)}</p>` : '';
  const attrs = ROW_KEYS.map(key => `data-${key.replace(/_/g, '-')}="${escapeHtml(String(job[key] ?? ''))}"`).join(' ');
  const rootAttr = `data-root-job-id="${escapeHtml(String(job.root_job_id || job.job_id))}"`;
  return `<article class="activity-row" ${attrs} ${rootAttr}>
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
    ${summaryHtml}
    <div class="activity-row-actions">
      <button type="button" class="icon-button" data-activity-cancel="${escapeHtml(job.job_id)}" ${job.can_cancel ? '' : 'disabled'}>${escapeHtml(t('activity.cancel'))}</button>
      <button type="button" class="icon-button" data-activity-retry="${escapeHtml(job.job_id)}" ${job.can_retry ? '' : 'hidden'}>${escapeHtml(t('activity.retry'))}</button>
      <button type="button" class="icon-button" data-activity-resume="${escapeHtml(job.job_id)}" ${job.can_resume ? '' : 'hidden'}>${escapeHtml(t('activity.resume'))}</button>
      <button type="button" class="icon-button" data-activity-toggle-items="${escapeHtml(job.job_id)}">${escapeHtml(expanded ? t('activity.hide_failures') : t('activity.show_failures'))}</button>
    </div>
    ${itemsHtml}
  </article>`;
}

function renderGroupedJobs(jobs) {
  const grouped = groupJobsByRoot(jobs);
  const container = $('activityGroupedList');
  const section = $('activityGroupedSection');
  if (!container || !section) return;
  if (grouped.size === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const html = [...grouped.entries()].map(([root, list]) => `
    <div class="activity-group" data-root-job-id="${escapeHtml(root)}">
      <h4 class="activity-group-title">${escapeHtml(t('activity.group_title', {run: root.slice(0, 8), count: list.length}))}</h4>
      <div class="activity-group-jobs">${list.map(renderJobRow).join('')}</div>
    </div>
  `).join('');
  container.innerHTML = html;
}

function renderActivity() {
  ensureActivityDrawer();
  localizeDrawer();
  let jobs = [...jobsById.values()];
  jobs = filterJobs(jobs, {typeFilter: activityTypeFilter || null, stateFilter: activityStateFilter || null});
  const {active, attention, recent} = partitionJobs(jobs);
  const sections = [
    ['activityActiveList', active, t('activity.empty_active')],
    ['activityAttentionList', attention, t('activity.empty_attention')],
    ['activityRecentList', recent, t('activity.empty_recent')],
  ];
  for (const [id, list, emptyMessage] of sections) {
    const el = $(id);
    if (!el) continue;
    if (!list.length) renderEmpty(el, emptyMessage);
    else el.innerHTML = list.map(renderJobRow).join('');
  }
  renderGroupedJobs(jobs);
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
  const next = {...existing, ...patch, job_id: jobId};
  jobsById.set(jobId, next);
  announceJobState(next, existing.state);
  updateActivityCount();
  if ($('activityDrawer')?.open) renderActivity();
}

function onJobSse(event) {
  let payload;
  try { payload = JSON.parse(event.data); } catch { return; }
  if (!payload?.job_id) return;
  mergeJobPatch(payload.job_id, payload);
  if (event.type === 'job.finished') {
    const job = jobsById.get(payload.job_id);
    if (job) {
      const line = jobSummaryLine({...job, ...payload});
      if (line) job.message = line;
    }
  }
}

function connectActivitySse() {
  // The shared stream (static/events.js) owns the EventSource, reconnect
  // backoff, and beforeunload close; this only maps job frames to the drawer.
  if (jobEventsBound) return;
  jobEventsBound = true;
  for (const kind of JOB_SSE_EVENTS) onServerEvent(kind, onJobSse);
  // After a reconnect, events may have been missed: resync from the snapshot.
  onServerEvent('reconnected', () => { refreshJobsSnapshot().catch(() => {}); });
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
    notify(t('activity.cancellation_requested'));
    await refreshJobsSnapshot();
  } catch (error) {
    notifyError(error, t('activity.cancel_failed'));
  }
}

async function retryJob(jobId) {
  try {
    await api('/api/v2/jobs/retry', {method: 'POST', body: JSON.stringify({job_id: jobId})});
    notify(t('activity.retry_queued'));
    await refreshJobsSnapshot();
  } catch (error) {
    notifyError(error, t('activity.retry_failed'));
  }
}

async function resumeJob(jobId) {
  try {
    await api('/api/v2/jobs/resume', {method: 'POST', body: JSON.stringify({job_id: jobId})});
    notify(t('activity.resume_queued'));
    await refreshJobsSnapshot();
  } catch (error) {
    notifyError(error, t('activity.resume_failed'));
  }
}

async function loadJobItems(jobId, {append = false} = {}) {
  const existing = itemPages.get(jobId) || {items: [], next_cursor: null};
  const cursor = append ? existing.next_cursor : null;
  // Bounded error pagination: limit clamped at 50 per spec
  const query = new URLSearchParams({job_id: jobId, limit: '50'});
  if (cursor) query.set('cursor', cursor);
  const page = await api(`/api/v2/jobs/items?${query.toString()}`);
  const items = append ? [...existing.items, ...(page.items || [])] : (page.items || []);
  // Enforce bounded storage to avoid unbounded memory growth
  const capped = items.slice(-200);
  itemPages.set(jobId, {items: capped, next_cursor: page.next_cursor || null});
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
  openDialog($('activityDrawer'), document.getElementById('activityButton') || document.activeElement);
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

// P8: JS-rendered drawer strings follow the locale instead of waiting for a
// reload. Static drawer chrome is refreshed by localizeDrawer().
onLocaleChange(() => {
  if ($('activityDrawer')) localizeDrawer();
  if ($('activityDrawer')?.open) renderActivity();
});

queueMicrotask(() => { initActivity().catch(() => {}); });
