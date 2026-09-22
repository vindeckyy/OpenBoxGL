/* Library health score UI (Flagship 8: H4 desktop dialog, H6 rescan toast).
 *
 * The score is measured by pkg/parity/parity_library_health.py and served
 * cached by /api/v2/library/health. This module renders the dashboard card
 * and the breakdown dialog, and runs the one-click fix queue with dry-run
 * previews. Fixes never run without explicit consent; every executed fix
 * records an undo token in the fix journal.
 */
import { $, escapeHtml } from './util.js';
import { AppState, api, notify, token, nativePickFolder } from './state.js';
import { t } from './i18n.js';
import { render } from './library.js';
import { confirmAction } from './dialogs.js';

const DIMENSIONS = ['file_integrity', 'duplicates', 'artwork', 'metadata', 'launch_readiness'];
const PAGE_SIZE = 25;
let _lastScore = null;
let _healthUndoTimer = null;
let _sseStarted = false;

export function dimensionLabel(dimension) {
  return t(`health.dim_${dimension}`, dimension);
}

export async function fetchHealthSnapshot() {
  const snapshot = await api('/api/v2/library/health');
  if (typeof snapshot?.score === 'number') _lastScore = snapshot.score;
  return snapshot;
}

function scoreClass(score) {
  return score >= 85 ? 'health-great' : score >= 60 ? 'health-ok' : 'health-poor';
}

function dimensionBar(dimension, info) {
  const sub = info?.score ?? 0;
  return `<div class="health-dim" data-health-dim="${dimension}">
    <button type="button" class="health-dim-head" data-health-toggle="${dimension}" aria-expanded="false">
      <span class="health-dim-name">${escapeHtml(dimensionLabel(dimension))}</span>
      <span class="health-bar"><span class="health-bar-fill ${scoreClass(sub)}" style="width:${sub}%"></span></span>
      <span class="health-dim-score">${sub}</span>
      <span class="health-dim-count">${info?.issues ?? 0} ${escapeHtml(t('health.issues'))}</span>
    </button>
    <div class="health-dim-body" data-health-body="${dimension}" hidden>
      <div class="health-issues" data-health-issues="${dimension}"></div>
      <div class="health-dim-actions">
        <button type="button" class="icon-button" data-health-more="${dimension}" hidden>${escapeHtml(t('health.load_more'))}</button>
        <button type="button" class="primary" data-health-fix="${dimension}">${escapeHtml(t('health.fix_all_dimension'))}</button>
      </div>
    </div>
  </div>`;
}

function issueRow(issue) {
  return `<button type="button" class="metadata-result icon-button" data-health-game="${issue.index ?? ''}" data-health-gid="${escapeHtml(issue.game_id || '')}">
    <div><strong>${escapeHtml(issue.name || t('health.unnamed'))}</strong><small>${escapeHtml(issue.reason)} · ${escapeHtml(issue.detail || '')}</small></div>
  </button>`;
}

async function loadIssuePage(dimension, offset) {
  const result = await api(`/api/v2/library/health/issues?dimension=${encodeURIComponent(dimension)}&limit=${PAGE_SIZE}&offset=${offset}`);
  const host = document.querySelector(`[data-health-issues="${dimension}"]`);
  if (!host) return;
  if (offset === 0) host.innerHTML = '';
  host.insertAdjacentHTML('beforeend', (result.issues || []).map(issueRow).join(''));
  host.querySelectorAll('[data-health-game]').forEach(button => {
    button.onclick = () => {
      const id = Number(button.dataset.healthGame);
      if (Number.isFinite(id)) AppState.selectedId = id;
      $('healthScoreDialog')?.close();
      render();
    };
  });
  const more = document.querySelector(`[data-health-more="${dimension}"]`);
  if (more) {
    const next = offset + PAGE_SIZE;
    more.hidden = next >= (result.total || 0);
    more.onclick = () => loadIssuePage(dimension, next);
  }
}

function bindDialog(snapshot) {
  const dialog = $('healthScoreDialog');
  dialog.querySelectorAll('[data-health-toggle]').forEach(button => {
    button.onclick = async () => {
      const dimension = button.dataset.healthToggle;
      const body = dialog.querySelector(`[data-health-body="${dimension}"]`);
      const open = body.hidden;
      body.hidden = !open;
      button.setAttribute('aria-expanded', String(open));
      if (open && !body.dataset.loaded) {
        body.dataset.loaded = '1';
        try { await loadIssuePage(dimension, 0); }
        catch (error) { notify(error.message); }
      }
    };
  });
  dialog.querySelectorAll('[data-health-fix]').forEach(button => {
    button.onclick = () => fixDimension(button.dataset.healthFix);
  });
}

export async function openHealthScore() {
  const dialog = $('healthScoreDialog');
  if (!dialog) return;
  $('closeHealthScore').onclick = () => dialog.close();
  $('doneHealthScore').onclick = () => dialog.close();
  $('healthScoreBody').innerHTML = `<p class="description">${escapeHtml(t('common.loading'))}</p>`;
  if (!dialog.open) dialog.showModal();
  try {
    const snapshot = await fetchHealthSnapshot();
    if (!snapshot.scanned) {
      $('healthScoreBody').innerHTML = `
        <p class="description">${escapeHtml(t('health.no_scan'))}</p>
        <div class="dialog-actions"><button type="button" class="primary" id="healthRescan">${escapeHtml(t('health.rescan'))}</button></div>`;
      $('healthRescan').onclick = () => rescanLibrary();
      return;
    }
    const dims = snapshot.dimensions || {};
    const worst = DIMENSIONS.reduce((acc, dim) => (!acc || (dims[dim]?.score ?? 100) < (dims[acc]?.score ?? 100) ? dim : acc), null);
    $('healthScoreBody').innerHTML = `
      <div class="health-hero">
        <div class="health-score ${scoreClass(snapshot.score)}">${snapshot.score}</div>
        <div class="health-hero-meta">
          <div><strong>${escapeHtml(t('health.title'))}</strong></div>
          <div class="description">${escapeHtml(t('health.computed', { count: snapshot.game_count }))} · ${escapeHtml(String(snapshot.computed_at || '').replace('T', ' '))}</div>
          ${worst ? `<div class="description">${escapeHtml(t('health.weakest', { dimension: dimensionLabel(worst) }))}</div>` : ''}
        </div>
        <button type="button" class="icon-button" id="healthRescan">${escapeHtml(t('health.rescan'))}</button>
      </div>
      <div class="health-dims">${DIMENSIONS.map(dim => dimensionBar(dim, dims[dim])).join('')}</div>
      <p class="description"><button type="button" class="link-button" id="healthHowScored">${escapeHtml(t('health.how_scored'))}</button></p>`;
    $('healthRescan').onclick = () => rescanLibrary();
    $('healthHowScored').onclick = () => notify(t('health.how_scored_detail'));
    bindDialog(snapshot);
  } catch (error) {
    $('healthScoreBody').innerHTML = `<p class="description">${escapeHtml(error.message)}</p>`;
  }
}

/** Compact card rendered at the top of the audit dialog (settings.js region). */
export async function renderHealthScoreCard() {
  const host = $('healthSummary');
  if (!host || host.querySelector('[data-health-card]')) return;
  const card = document.createElement('div');
  card.className = 'health-card';
  card.dataset.healthCard = '1';
  card.innerHTML = `<span class="health-card-label">${escapeHtml(t('health.title'))}</span><span class="health-card-score">…</span><button type="button" class="icon-button">${escapeHtml(t('health.details'))}</button>`;
  card.querySelector('button').onclick = () => { $('healthDialog')?.close(); openHealthScore(); };
  host.prepend(card);
  try {
    const snapshot = await fetchHealthSnapshot();
    const score = card.querySelector('.health-card-score');
    if (snapshot.scanned) {
      score.textContent = snapshot.score;
      score.classList.add(scoreClass(snapshot.score));
    } else {
      score.textContent = '–';
    }
  } catch { /* card stays; the dialog button still opens the score view */ }
}

export async function rescanLibrary() {
  try {
    const queued = await api('/api/v2/library/health/scan', { method: 'POST', body: '{}' });
    notify(t('health.scan_queued'));
    return queued;
  } catch (error) { notify(error.message); return null; }
}

// ── Fix queue ────────────────────────────────────────────────────────────────

function showHealthUndoToast(message, fixId, undoKind) {
  const toast = $('toast');
  if (!toast) { notify(message); return; }
  clearTimeout(_healthUndoTimer);
  if (notify.timer) clearTimeout(notify.timer);
  toast.dataset.notifyLevel = 'info';
  toast.innerHTML = `<span class="trash-toast-text">${escapeHtml(message)}</span><button type="button" class="trash-undo" id="healthUndoButton">${escapeHtml(t('trash.undo'))}</button>`;
  toast.classList.add('show');
  $('healthUndoButton').onclick = async () => {
    toast.classList.remove('show');
    try {
      await api('/api/v2/library/health/undo', { method: 'POST', body: JSON.stringify({ fix_id: fixId }) });
      notify(t('health.undone'));
      render();
      openHealthScore();
    } catch (error) { notify(error.message); }
  };
  _healthUndoTimer = setTimeout(() => toast.classList.remove('show'), 8000);
}

async function fixDimension(dimension) {
  let preview;
  try {
    let body = { dimension, issue_ids: 'all', dry_run: true };
    if (dimension === 'file_integrity') {
      const folder = await nativePickFolder().catch(() => null);
      if (!folder) { notify(t('health.need_folder')); return; }
      body.folder = folder;
    }
    preview = await api('/api/v2/library/health/fix', { method: 'POST', body: JSON.stringify(body) });
  } catch (error) { notify(error.message); return; }
  const plan = preview.plan || {};
  if (plan.action === 'open_editor' || plan.action === 'open_emulator_profiles') {
    notify(plan.detail || t('health.manual_fix'));
    return;
  }
  const target = plan.merges ? t('health.preview_merges', { count: plan.merges.length })
    : plan.targets ? t('health.preview_targets', { count: plan.targets.length })
    : plan.matches ? t('health.preview_matches', { count: plan.matches.length })
    : t('health.preview_unknown');
  const ok = await confirmAction({
    title: t('health.fix_title', { dimension: dimensionLabel(dimension) }),
    message: target,
    consequence: t('health.fix_consequence'),
    recovery: t('health.fix_recovery'),
    confirmLabel: t('health.fix_confirm'),
    destructive: dimension === 'duplicates',
  });
  if (!ok) return;
  try {
    const result = await api('/api/v2/library/health/fix', {
      method: 'POST',
      body: JSON.stringify({ dimension, issue_ids: 'all', dry_run: false, base_token: preview.base_token, folder: preview.plan?.folder }),
    });
    if (result.executed === false) { notify(result.detail || t('health.manual_fix')); return; }
    const summary = result.summary || {};
    const doneMsg = t('health.fix_done', { count: summary.merged ?? summary.updated ?? summary.games ?? 0 });
    if (result.undo?.fix_id) showHealthUndoToast(doneMsg, result.undo.fix_id, result.undo.kind);
    else notify(doneMsg);
    render();
  } catch (error) { notify(error.message); }
}

// ── H6: scheduled-rescan toast with score delta ──────────────────────────────

export function initHealthSse() {
  if (_sseStarted) return;
  _sseStarted = true;
  const connect = () => {
    let source = null;
    try {
      source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
    } catch { return; }
    source.addEventListener('job.finished', async event => {
      let payload;
      try { payload = JSON.parse(event.data); } catch { return; }
      if (payload?.name !== 'library-health-scan') return;
      try {
        const before = _lastScore;
        const snapshot = await fetchHealthSnapshot();
        if (typeof before === 'number' && before !== snapshot.score) {
          notify(t('health.rescan_done', { before, after: snapshot.score }));
        } else {
          notify(t('health.rescan_done_same', { score: snapshot.score }));
        }
      } catch { /* toast is best-effort */ }
    });
    source.onerror = () => { try { source.close(); } catch { /* noop */ } };
  };
  connect();
}
