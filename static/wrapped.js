/* wrapped.js — annual gaming report (printable). */
import { $ } from './util.js';
import { t } from './i18n.js';
import { AppState, api } from './state.js';

let dialog;

function openWrapped(year = new Date().getFullYear()) {
  if (!dialog) initDom();
  dialog.showModal();
  loadWrapped(year);
}

function initDom() {
  dialog = $('wrappedDialog');
  $('closeWrapped').onclick = () => dialog.close();
  $('wrappedPrint').onclick = () => window.print();
  $('wrappedYear').onchange = (e) => loadWrapped(parseInt(e.target.value, 10));
}

async function loadWrapped(year) {
  const body = $('wrappedBody');
  body.innerHTML = `<p data-i18n="common.loading">${t('common.loading')}</p>`;
  try {
    const data = await api(`/api/v2/insights/wrapped?year=${year}`);
    render(data);
  } catch (e) {
    body.innerHTML = `<p class="description">${e.message}</p>`;
  }
}

function fmtDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function render(data) {
  const top = data.top || {};
  const totals = data.totals || {};
  const progress = data.progress || {};
  const body = $('wrappedBody');
  body.innerHTML = `
    <div class="wrapped-cards">
      <div class="wrapped-card"><span class="wrapped-stat">${fmtDuration(totals.playtime_seconds || 0)}</span><span data-i18n="wrapped.total_playtime">${t('wrapped.total_playtime')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${totals.sessions || 0}</span><span data-i18n="wrapped.sessions">${t('wrapped.sessions')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${totals.games_played || 0}</span><span data-i18n="wrapped.games_played">${t('wrapped.games_played')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${totals.new_games || 0}</span><span data-i18n="wrapped.new_games">${t('wrapped.new_games')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${(data.streak || {}).longest || 0}</span><span data-i18n="wrapped.longest_streak">${t('wrapped.longest_streak')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${progress.beaten || 0}</span><span data-i18n="wrapped.beaten">${t('wrapped.beaten')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${progress.completed || 0}</span><span data-i18n="wrapped.completed">${t('wrapped.completed')}</span></div>
      <div class="wrapped-card"><span class="wrapped-stat">${progress.mastered || 0}</span><span data-i18n="wrapped.mastered">${t('wrapped.mastered')}</span></div>
    </div>
    <div class="wrapped-tops">
      <div><strong data-i18n="wrapped.top_game">${t('wrapped.top_game')}</strong><p>${(top.game || {}).name || '—'}</p></div>
      <div><strong data-i18n="wrapped.top_platform">${t('wrapped.top_platform')}</strong><p>${(top.platform || {}).platform || '—'}</p></div>
      <div><strong data-i18n="wrapped.top_genre">${t('wrapped.top_genre')}</strong><p>${(top.genre || {}).genre || '—'}</p></div>
    </div>
    <div class="wrapped-first">
      <div><strong data-i18n="wrapped.first_play">${t('wrapped.first_play')}</strong><p>${(data.first_play || {}).name || '—'}</p></div>
      <div><strong data-i18n="wrapped.oldest_played">${t('wrapped.oldest_played')}</strong><p>${(data.oldest_played || {}).name || '—'}</p></div>
    </div>
    <div class="wrapped-co-play">
      <strong data-i18n="wrapped.co_play_pairs">${t('wrapped.co_play_pairs')}</strong>
      <p>${data.co_play_pairs || 0}</p>
    </div>
  `;
}

export { openWrapped };
