/* mastery.js — completionist dashboard. */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { AppState, api } from './state.js';

const STATES = ['never', 'played', 'beaten', 'completed', 'mastered'];
function stateLabels() {
  return {
    never: t('mastery.never'),
    played: t('mastery.played'),
    beaten: t('mastery.beaten'),
    completed: t('mastery.completed'),
    mastered: t('mastery.mastered'),
  };
}
const STATE_COLORS = {
  never: 'var(--mastery-never)',
  played: 'var(--mastery-played)',
  beaten: 'var(--mastery-beaten)',
  completed: 'var(--mastery-completed)',
  mastered: 'var(--mastery-mastered)',
};

let dialog;

function openMastery() {
  if (!dialog) initDom();
  dialog.showModal();
  loadMastery();
}

function initDom() {
  dialog = $('masteryDialog');
  $('closeMastery').onclick = () => dialog.close();
  $('masteryDecadeFilter').onchange = () => loadMastery();
}

async function loadMastery() {
  const body = $('masteryBody');
  body.innerHTML = `<p data-i18n="common.loading">${t('common.loading')}</p>`;
  try {
    const data = await api('/api/v2/insights/mastery');
    render(data);
  } catch (e) {
    body.innerHTML = `<p class="description">${escapeHtml(e.message)}</p>`;
  }
}

function render(data) {
  const filter = $('masteryDecadeFilter').value;
  const labels = stateLabels();
  const useDecades = filter !== '';
  const rows = useDecades ? Object.entries(data.decades || {}).sort() : Object.entries(data.platforms || {}).sort();

  // D5: RA-cache-missing affordance uses only the already-fetched mastery
  // payload (zero new network calls); local progress is always available.
  const hasRa = Boolean(data.ra_available) || Object.values(data.platforms || {}).some(s => (s.ra_tracked || 0) > 0);
  let html = '';
  if (!hasRa) {
    html += `<p class="description" data-i18n="mastery.local_only">${t('mastery.local_only')}</p>`;
  }
  html += '<div class="mastery-list">';
  for (const [name, stats] of rows) {
    if (useDecades && filter !== 'all' && name !== filter) continue;
    const total = stats.total || 0;
    if (!total) continue;
    html += `<div class="mastery-row" data-name="${escapeHtml(name)}">`;
    html += `<div class="mastery-label">${escapeHtml(name)} <span class="mastery-count">(${total})</span></div>`;
    html += '<div class="mastery-bar">';
    for (const state of STATES) {
      const count = stats[state] || 0;
      const pct = (count / total) * 100;
      if (!count) continue;
      html += `<div class="mastery-segment" data-state="${state}" data-name="${escapeHtml(name)}" style="width:${pct.toFixed(2)}%; background:${STATE_COLORS[state]}" title="${labels[state]}: ${count}"></div>`;
    }
    html += '</div>';
    html += '<div class="mastery-ra">';
    if (stats.ra_tracked) {
      html += `${t('mastery.ra_progress')}: ${stats.ra_avg_progress}% (${stats.ra_mastered}/${stats.ra_tracked})`;
    }
    html += '</div></div>';
  }
  html += '</div>';

  $('masteryOverall').innerHTML = renderOverall(data.overall || {});
  $('masteryBody').innerHTML = html;

  $('masteryBody').querySelectorAll('.mastery-segment').forEach(seg => {
    seg.onclick = () => {
      const name = seg.dataset.name;
      const state = seg.dataset.state;
      document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: null } }));
      // Filter the library by platform (or decade in a real implementation)
      if (AppState) AppState.platformFilter = name;
    };
  });
}

function renderOverall(stats) {
  const total = stats.total || 0;
  const labels = stateLabels();
  if (!total) return `<p data-i18n="mastery.empty">${t('mastery.empty')}</p>`;
  let html = `<div class="mastery-overall"><div class="mastery-bar">`;
  for (const state of STATES) {
    const count = stats[state] || 0;
    const pct = (count / total) * 100;
    if (!count) continue;
    html += `<div class="mastery-segment" style="width:${pct.toFixed(2)}%; background:${STATE_COLORS[state]}" title="${labels[state]}: ${count}"></div>`;
  }
  html += `</div><div class="mastery-ra">`;
  if (stats.ra_tracked) html += `${t('mastery.ra_progress')}: ${stats.ra_avg_progress}%`;
  html += '</div></div>';
  return html;
}

export { openMastery };
