/* dna.js — Game DNA smart search (Flagship 9).
   Title | Smart toggle on the sidebar search box (default: Title, persisted).
   Smart mode queries POST /api/v2/library/dna/search with a 250 ms debounce
   and renders a results panel with "why" explanation chips. 100% local:
   classical BM25 + concept lexicon + feature hashing — no AI cloud, no downloads.
   No dependencies beyond the app's own modules.
*/
import { $, escapeHtml } from './util.js';
import { AppState, api, notify } from './state.js';
import { t } from './i18n.js';

const DEBOUNCE_MS = 250;
const RESULT_LIMIT = 25;
const BUILD_POLL_MS = 1500;

let _timer = null;
let _pollTimer = null;
let _seq = 0;

export function dnaSearchMode() {
  const saved = AppState.appSettings && AppState.appSettings.dna_search_mode;
  return saved === 'smart' ? 'smart' : 'title';
}

export function setDnaSearchMode(next, { search = true, persist = true } = {}) {
  const prev = dnaSearchMode();
  if (AppState.appSettings) AppState.appSettings.dna_search_mode = next;
  document.querySelectorAll('[data-dna-mode]').forEach(btn => {
    const active = btn.dataset.dnaMode === next;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
    if (btn.id === 'bigBoxDnaToggle') {
      btn.textContent = next === 'smart' ? t('dna.mode_smart') : t('dna.mode_title');
      btn.title = t('dna.privacy_note');
    }
  });
  const input = $('sidebarSearch');
  if (input) input.placeholder = next === 'smart' ? t('dna.search_placeholder_smart') : t('sidebar.search');
  if (persist) {
    api('/api/settings', { method: 'POST', body: JSON.stringify({ dna_search_mode: next }) }).catch(() => {});
  }
  if (next === 'smart') {
    if (!search) return;
    const q = (input && input.value || '').trim();
    if (q) scheduleSmartSearch(q);
    else hidePanel();
  } else {
    hidePanel();
    // Re-run the existing title search so the grid refreshes immediately.
    if (prev !== 'title' && input) input.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

function syncToggle() {
  setDnaSearchMode(dnaSearchMode(), { search: false, persist: false });
}

function ensureToggle() {
  const input = $('sidebarSearch');
  if (!input || $('dnaModeToggle')) return;
  const wrap = document.createElement('div');
  wrap.className = 'dna-search-wrap';
  input.replaceWith(wrap);
  wrap.appendChild(input);
  const toggle = document.createElement('div');
  toggle.className = 'dna-mode-toggle';
  toggle.id = 'dnaModeToggle';
  toggle.setAttribute('role', 'group');
  toggle.setAttribute('aria-label', t('dna.mode_label'));
  toggle.title = t('dna.privacy_note');
  toggle.innerHTML =
    `<button type="button" class="dna-mode" data-dna-mode="title" aria-pressed="true">${escapeHtml(t('dna.mode_title'))}</button>` +
    `<button type="button" class="dna-mode" data-dna-mode="smart" aria-pressed="false">${escapeHtml(t('dna.mode_smart'))}</button>`;
  toggle.addEventListener('click', event => {
    const btn = event.target.closest('[data-dna-mode]');
    if (btn) setDnaSearchMode(btn.dataset.dnaMode);
  });
  wrap.appendChild(toggle);
  syncToggle();
}

function panel() {
  let el = $('dnaResults');
  if (!el) {
    el = document.createElement('div');
    el.id = 'dnaResults';
    el.className = 'dna-results';
    el.hidden = true;
    el.setAttribute('role', 'listbox');
    el.setAttribute('aria-label', t('dna.results_title'));
    document.querySelector('.dna-search-wrap')?.appendChild(el);
    document.addEventListener('click', event => {
      if (!event.target.closest?.('.dna-search-wrap')) hidePanel();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && el && !el.hidden) hidePanel();
    });
  }
  return el;
}

function hidePanel() {
  const el = $('dnaResults');
  if (el) el.hidden = true;
  stopBuildPoll();
}

function showPanel(html) {
  const el = panel();
  el.innerHTML = html;
  el.hidden = false;
}

function chipHtml(chips) {
  return (chips || []).map(c => `<span class="dna-chip">${escapeHtml(c)}</span>`).join('');
}

function renderResults(payload) {
  const results = payload.results || [];
  if (payload.building) return renderBuilding(payload);
  if (!results.length) {
    const coverage = payload.coverage_pct ?? 0;
    if (coverage <= 0) {
      showPanel(`<div class="dna-state"><p>${escapeHtml(t('dna.empty_coverage'))}</p>` +
        `<button type="button" class="icon-button" id="dnaOpenMetadata">${escapeHtml(t('dna.open_metadata'))}</button>` +
        `<p class="dna-privacy">${escapeHtml(t('dna.privacy_note'))}</p></div>`);
      $('dnaOpenMetadata')?.addEventListener('click', () => { hidePanel(); $('metadataButton')?.click(); });
    } else {
      showPanel(`<div class="dna-state"><p>${escapeHtml(t('dna.no_results'))}</p>` +
        `<p class="dna-privacy">${escapeHtml(t('dna.privacy_note'))}</p></div>`);
    }
    return;
  }
  const degraded = payload.degraded
    ? `<p class="dna-degraded">${escapeHtml(t('dna.degraded_note'))}</p>` : '';
  showPanel(degraded + results.map(r =>
    `<button type="button" class="dna-result" role="option" data-game-id="${escapeHtml(String(r.game_id ?? ''))}">` +
    `<span class="dna-result-name">${escapeHtml(r.name || '')}</span>` +
    `<span class="dna-chips">${chipHtml(r.why)}</span></button>`
  ).join('') + `<p class="dna-privacy">${escapeHtml(t('dna.privacy_note'))}</p>`);
  panel().querySelectorAll('.dna-result').forEach(btn => {
    btn.addEventListener('click', () => {
      hidePanel();
      const gameId = btn.dataset.gameId;
      if (gameId) {
        document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId } }));
      }
    });
  });
}

function stopBuildPoll() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function renderBuilding(payload) {
  const jobId = payload.job_id;
  showPanel(`<div class="dna-state"><p>${escapeHtml(t('dna.building', { pct: 0 }))}</p>` +
    (jobId ? `<button type="button" class="icon-button" id="dnaCancelBuild">${escapeHtml(t('dna.cancel_build'))}</button>` : '') +
    `<p class="dna-privacy">${escapeHtml(t('dna.privacy_note'))}</p></div>`);
  if (jobId) {
    $('dnaCancelBuild')?.addEventListener('click', async () => {
      try { await api('/api/v2/jobs/cancel', { method: 'POST', body: JSON.stringify({ job_id: jobId }) }); } catch {}
      hidePanel();
    });
  }
  stopBuildPoll();
  _pollTimer = setInterval(async () => {
    try {
      const jobs = await api('/api/jobs');
      const job = jobs && jobs.jobs && jobs.jobs['dna-index-rebuild'];
      const pct = job && job.total ? Math.round(100 * (job.processed || 0) / job.total) : null;
      const label = $('dnaResults')?.querySelector('.dna-state p');
      if (label && pct !== null) label.textContent = t('dna.building', { pct });
      if (!job || ['done', 'error', 'cancelled'].includes(job.state)) {
        stopBuildPoll();
        const q = ($('sidebarSearch')?.value || '').trim();
        if (q && dnaSearchMode() === 'smart') runSmartSearch(q);
        else hidePanel();
      }
    } catch { stopBuildPoll(); }
  }, BUILD_POLL_MS);
}

async function runSmartSearch(query) {
  const mySeq = ++_seq;
  const el = panel();
  try {
    const payload = await api('/api/v2/library/dna/search', {
      method: 'POST',
      body: JSON.stringify({ query, limit: RESULT_LIMIT }),
    });
    if (mySeq !== _seq) return;
    if (dnaSearchMode() !== 'smart') return;
    renderResults(payload || {});
  } catch (error) {
    if (mySeq !== _seq) return;
    showPanel(`<div class="dna-state"><p>${escapeHtml(error.message || t('dna.search_failed'))}</p></div>`);
    notify(error.message);
  }
}

function scheduleSmartSearch(query) {
  clearTimeout(_timer);
  _timer = setTimeout(() => runSmartSearch(query), DEBOUNCE_MS);
}

/** "More like this": switch to Smart mode and search for games like `name`. */
export function dnaMoreLikeThis(name) {
  const input = $('sidebarSearch');
  if (!input) return;
  setDnaSearchMode('smart');
  input.value = `games like ${name}`;
  input.focus();
  runSmartSearch(input.value.trim());
}

/* Big Box (couch) integration: feeds the existing #bigBoxHybridSearch input,
   no on-screen keyboard work. Why-chips render as subtitle lines. */
let _bbTimer = null;

export function cancelBigBoxSmartSearch() {
  if (_bbTimer) { clearTimeout(_bbTimer); _bbTimer = null; }
}

export function scheduleBigBoxSmartSearch(query, filters, onDone) {
  cancelBigBoxSmartSearch();
  _bbTimer = setTimeout(async () => {
    _bbTimer = null;
    try {
      const payload = await api('/api/v2/library/dna/search', {
        method: 'POST',
        body: JSON.stringify({ query, limit: 50, filters: filters || {} }),
      });
      onDone(null, payload || {});
    } catch (error) {
      onDone(error, null);
    }
  }, DEBOUNCE_MS);
}

export function dnaMoreLikeThisBigBox(name) {
  setDnaSearchMode('smart', { search: false });
  const input = $('bigBoxHybridSearch');
  if (!input) return;
  input.value = `games like ${name}`;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
}

export function initDnaSearch() {
  ensureToggle();
  const input = $('sidebarSearch');
  if (!input || input.dataset.dnaBound) return;
  input.dataset.dnaBound = '1';
  input.addEventListener('input', () => {
    if (dnaSearchMode() !== 'smart') { hidePanel(); return; }
    const q = input.value.trim();
    if (!q) { hidePanel(); return; }
    scheduleSmartSearch(q);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('dnaResults')?.hidden) hidePanel();
  });
}
