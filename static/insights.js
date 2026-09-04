/* Play Insights — 1.7.1, wired in 1.8.0
   Renders GitHub-like heatmap + streak + top platforms/genres/games into #insightsPanel.
   Uses app.css tokens only. No deps. Lazy-loads via /api/v2/insights/summary when
   the panel first scrolls into view; reloads on app:state-refreshed while visible.
   Table fallback for a11y.
*/
import { $, escapeHtml } from './util.js';
import { api, AppState } from './state.js';
import { t } from './i18n.js';
import { openWrapped } from './wrapped.js';

const LEVEL_CLASS = ['level-0', 'level-1', 'level-2', 'level-3', 'level-4'];
const RANGE_KEY = 'openbox-insights-range';
const REFRESH_DEBOUNCE_MS = 2000;

let rangeDays = 365;
let refreshTimer = 0;
let observer = null;
let loaded = false;

function formatHours(seconds) {
  if (!seconds) return '0h';
  const hours = seconds / 3600;
  if (hours < 1) return `${Math.round(seconds / 60)}m`;
  if (hours < 10) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours)}h`;
}

function storedRange() {
  try {
    const value = Number(localStorage.getItem(RANGE_KEY));
    if ([30, 90, 365].includes(value)) return value;
  } catch { /* storage unavailable */ }
  return 365;
}

function renderHeatmap(heatmap) {
  const cells = heatmap.map(cell => {
    const cls = LEVEL_CLASS[Math.max(0, Math.min(4, cell.level || 0))] || 'level-0';
    const title = `${cell.date}: ${cell.count} session${cell.count === 1 ? '' : 's'} • ${formatHours(cell.seconds)}`;
    return `<div class="insight-cell ${cls}" data-date="${escapeHtml(cell.date)}" role="gridcell" aria-label="${escapeHtml(title)}" title="${escapeHtml(title)}"></div>`;
  }).join('');
  const tableRows = heatmap.map(cell => `<tr><td>${escapeHtml(cell.date)}</td><td>${cell.count}</td><td>${escapeHtml(formatHours(cell.seconds))}</td><td>${cell.level}</td></tr>`).join('');
  const label = t('insights.heatmap_label', { days: rangeDays });
  return `
    <div class="insight-heatmap" role="grid" aria-label="${escapeHtml(label)}">${cells}</div>
    <table class="insight-heatmap-table sr-only" aria-label="${escapeHtml(label)}">
      <thead><tr><th>${escapeHtml(t('insights.col_date'))}</th><th>${escapeHtml(t('insights.col_sessions'))}</th><th>${escapeHtml(t('insights.col_playtime'))}</th><th>${escapeHtml(t('insights.col_level'))}</th></tr></thead>
      <tbody>${tableRows}</tbody>
    </table>
  `;
}

function renderTotals(totals) {
  return `
    <div class="insight-cards">
      <div class="insight-card"><div class="insight-card-value">${totals.games}</div><div class="insight-card-label">${escapeHtml(t('insights.games'))}</div></div>
      <div class="insight-card"><div class="insight-card-value">${totals.played}</div><div class="insight-card-label">${escapeHtml(t('insights.played'))}</div></div>
      <div class="insight-card"><div class="insight-card-value">${escapeHtml(formatHours(totals.total_playtime_seconds))}</div><div class="insight-card-label">${escapeHtml(t('insights.playtime'))}</div></div>
      <div class="insight-card"><div class="insight-card-value">${totals.total_sessions}</div><div class="insight-card-label">${escapeHtml(t('insights.sessions'))}</div></div>
    </div>
  `;
}

function renderTopList(title, items, keyName) {
  if (!items.length) return `<div class="insight-section"><h3 class="insight-title">${escapeHtml(title)}</h3><p class="muted">${escapeHtml(t('insights.no_data'))}</p></div>`;
  const rows = items.map(item => {
    const label = escapeHtml(item[keyName] || 'Unknown');
    const count = item.count ?? 0;
    const hours = item.playtime_seconds !== undefined ? ` • ${escapeHtml(formatHours(item.playtime_seconds))}` : '';
    return `<li class="insight-rank-row"><span class="insight-rank-label">${label}</span><span class="insight-rank-count">${count}${hours}</span></li>`;
  }).join('');
  return `<div class="insight-section"><h3 class="insight-title">${escapeHtml(title)}</h3><ol class="insight-rank">${rows}</ol></div>`;
}

function renderTopGames(items) {
  if (!items.length) return `<div class="insight-section"><h3 class="insight-title">${escapeHtml(t('insights.top_games'))}</h3><p class="muted">${escapeHtml(t('insights.no_data'))}</p></div>`;
  const rows = items.map(item => {
    const label = escapeHtml(item.name || 'Unknown');
    const hours = ` • ${escapeHtml(formatHours(item.playtime_seconds))}`;
    return `<li class="insight-rank-row"><button type="button" class="insight-game-link" data-insight-game="${escapeHtml(item.game_id)}"><span class="insight-rank-label">${label}</span><span class="insight-rank-count">${item.play_count ?? 0}${hours}</span></button></li>`;
  }).join('');
  return `<div class="insight-section"><h3 class="insight-title">${escapeHtml(t('insights.top_games'))}</h3><ol class="insight-rank">${rows}</ol></div>`;
}

function renderStreak(streak, momentum) {
  const delta = momentum.delta_seconds;
  const deltaLabel = delta === 0 ? t('insights.momentum_same') : (delta > 0 ? `+${formatHours(delta)} ${t('insights.momentum_vs_prev')}` : `${formatHours(delta)} ${t('insights.momentum_vs_prev')}`);
  const flame = streak.current >= 3 ? ' 🔥' : '';
  return `
    <div class="insight-section">
      <h3 class="insight-title">${escapeHtml(t('insights.streak_title'))}</h3>
      <div class="insight-streak">
        <span class="insight-streak-current">${streak.current}${escapeHtml(t('insights.day_streak'))}${escapeHtml(flame)}</span>
        <span class="insight-streak-longest">${escapeHtml(t('insights.longest'))}: ${streak.longest} ${escapeHtml(t('insights.days'))}</span>
        ${streak.last_played ? `<span class="insight-streak-last">${escapeHtml(t('insights.last'))}: ${escapeHtml(streak.last_played)}</span>` : ''}
        <span class="insight-momentum">${escapeHtml(t('insights.last_30d'))}: ${escapeHtml(formatHours(momentum.last_30_days_seconds))} • ${escapeHtml(deltaLabel)}</span>
      </div>
    </div>
  `;
}

async function loadInsights() {
  const panel = $('insightsPanel');
  if (!panel) return;
  const show = AppState.appSettings ? (AppState.appSettings.show_insights !== false) : true;
  if (!show) {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = '';
  ensurePanelHeader();
  const body = $('insightsBody');
  if (!body) return;
  body.innerHTML = `<div class="muted">${escapeHtml(t('insights.loading'))}</div>`;
  try {
    const data = await api(`/api/v2/insights/summary?days=${rangeDays}`);
    const { heatmap, totals, top_platforms, top_genres, top_games, streak, momentum } = data;
    body.innerHTML = `
      <div class="insights">
        ${renderTotals(totals)}
        ${renderStreak(streak, momentum)}
        <div class="insight-section">
          <h3 class="insight-title">${escapeHtml(t('insights.last_year'))}</h3>
          ${renderHeatmap(heatmap)}
          <div class="insight-legend" aria-hidden="true"><span>${escapeHtml(t('insights.less'))}</span><span class="insight-cell level-0"></span><span class="insight-cell level-1"></span><span class="insight-cell level-2"></span><span class="insight-cell level-3"></span><span class="insight-cell level-4"></span><span>${escapeHtml(t('insights.more'))}</span></div>
        </div>
        <div class="insight-two-col">
          ${renderTopList(t('insights.top_platforms'), top_platforms || [], 'platform')}
          ${renderTopList(t('insights.top_genres'), top_genres || [], 'genre')}
        </div>
        ${renderTopGames(top_games || [])}
      </div>
    `;
    loaded = true;
  } catch (error) {
    body.innerHTML = `<div class="muted">${escapeHtml(t('insights.unavailable'))}: ${escapeHtml(error.message || String(error))}</div>`;
  }
}

function ensurePanelHeader(force) {
  const panel = $('insightsPanel');
  if (!panel || ($('insightsBody') && !force)) return;
  panel.innerHTML = `
    <div class="insights-head">
      <h3 class="insights-title">${escapeHtml(t('library.play_insights'))}</h3>
      <div class="insights-head-actions">
        <button type="button" class="icon-button" id="insightsWrapped" data-i18n="wrapped.open">${t('wrapped.open')}</button>
        <select class="search insights-range" id="insightsRange" aria-label="${escapeHtml(t('insights.range_label'))}">
          <option value="30">${escapeHtml(t('insights.range_30'))}</option>
          <option value="90">${escapeHtml(t('insights.range_90'))}</option>
          <option value="365">${escapeHtml(t('insights.range_365'))}</option>
        </select>
        <button type="button" class="icon-button" id="insightsRefresh" aria-label="${escapeHtml(t('insights.refresh'))}">↻</button>
      </div>
    </div>
    <div id="insightsBody"></div>
  `;
  $('insightsRange').value = String(rangeDays);
  $('insightsRange').onchange = () => {
    rangeDays = Number($('insightsRange').value) || 365;
    try { localStorage.setItem(RANGE_KEY, String(rangeDays)); } catch { /* storage unavailable */ }
    loadInsights();
  };
  $('insightsRefresh').onclick = () => loadInsights();
  $('insightsWrapped').onclick = () => openWrapped();
  panel.addEventListener('click', event => {
    const link = event.target.closest('[data-insight-game]');
    if (!link) return;
    document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: link.dataset.insightGame } }));
  });
}

function bindInsights() {
  rangeDays = storedRange();
  const panel = $('insightsPanel');
  if (panel && typeof IntersectionObserver === 'function' && !observer) {
    observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting) && !loaded) {
        loaded = true;
        loadInsights();
      }
    }, { rootMargin: '160px' });
    observer.observe(panel);
  }
  document.addEventListener('app:state-refreshed', () => {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      refreshTimer = 0;
      const target = $('insightsPanel');
      if (target && target.offsetParent !== null && loaded) loadInsights();
    }, REFRESH_DEBOUNCE_MS);
  });
  // The locale dictionary arrives after boot; re-render strings that were
  // built with raw keys before it landed.
  document.addEventListener('localechange', () => {
    if ($('insightsBody')) {
      ensurePanelHeader(true);
      if (loaded) loadInsights();
    }
  });
}

export { loadInsights, bindInsights };
