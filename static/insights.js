/* Play Insights — 1.7.1
   Renders GitHub-like heatmap + streak + top platforms/genres into #insightsPanel.
   Uses app.css tokens only. No deps. Lazy-loads via /api/v2/insights/summary.
   Table fallback for a11y.
*/
import { $, escapeHtml } from './util.js';
import { api, AppState } from './state.js';

const LEVEL_CLASS = ['level-0', 'level-1', 'level-2', 'level-3', 'level-4'];

function formatHours(seconds) {
  if (!seconds) return '0h';
  const hours = seconds / 3600;
  if (hours < 1) return `${Math.round(seconds / 60)}m`;
  if (hours < 10) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours)}h`;
}

function renderHeatmap(heatmap) {
  const cells = heatmap.map(cell => {
    const cls = LEVEL_CLASS[Math.max(0, Math.min(4, cell.level || 0))] || 'level-0';
    const title = `${cell.date}: ${cell.count} session${cell.count === 1 ? '' : 's'} • ${formatHours(cell.seconds)}`;
    return `<div class="insight-cell ${cls}" data-date="${escapeHtml(cell.date)}" role="gridcell" aria-label="${escapeHtml(title)}" title="${escapeHtml(title)}"></div>`;
  }).join('');
  const tableRows = heatmap.map(cell => `<tr><td>${escapeHtml(cell.date)}</td><td>${cell.count}</td><td>${escapeHtml(formatHours(cell.seconds))}</td><td>${cell.level}</td></tr>`).join('');
  return `
    <div class="insight-heatmap" role="grid" aria-label="Play heatmap, last 366 days">${cells}</div>
    <table class="insight-heatmap-table sr-only" aria-label="Play heatmap table fallback">
      <thead><tr><th>Date</th><th>Sessions</th><th>Playtime</th><th>Level</th></tr></thead>
      <tbody>${tableRows}</tbody>
    </table>
  `;
}

function renderTotals(totals) {
  return `
    <div class="insight-cards">
      <div class="insight-card"><div class="insight-card-value">${totals.games}</div><div class="insight-card-label">Games</div></div>
      <div class="insight-card"><div class="insight-card-value">${totals.played}</div><div class="insight-card-label">Played</div></div>
      <div class="insight-card"><div class="insight-card-value">${escapeHtml(formatHours(totals.total_playtime_seconds))}</div><div class="insight-card-label">Playtime</div></div>
      <div class="insight-card"><div class="insight-card-value">${totals.total_sessions}</div><div class="insight-card-label">Sessions</div></div>
    </div>
  `;
}

function renderTopList(title, items, keyName) {
  if (!items.length) return `<div class="insight-section"><h3 class="insight-title">${escapeHtml(title)}</h3><p class="muted">No data yet — play something!</p></div>`;
  const rows = items.map(item => {
    const label = escapeHtml(item[keyName] || 'Unknown');
    const count = item.count ?? 0;
    const hours = item.playtime_seconds !== undefined ? ` • ${escapeHtml(formatHours(item.playtime_seconds))}` : '';
    return `<li class="insight-rank-row"><span class="insight-rank-label">${label}</span><span class="insight-rank-count">${count}${hours}</span></li>`;
  }).join('');
  return `<div class="insight-section"><h3 class="insight-title">${escapeHtml(title)}</h3><ol class="insight-rank">${rows}</ol></div>`;
}

function renderStreak(streak, momentum) {
  const delta = momentum.delta_seconds;
  const deltaLabel = delta === 0 ? 'same as previous 30d' : (delta > 0 ? `+${formatHours(delta)} vs previous 30d` : `${formatHours(delta)} vs previous 30d`);
  const flame = streak.current >= 3 ? ' 🔥' : '';
  return `
    <div class="insight-section">
      <h3 class="insight-title">Streak & momentum</h3>
      <div class="insight-streak">
        <span class="insight-streak-current">${streak.current}-day streak${escapeHtml(flame)}</span>
        <span class="insight-streak-longest">Longest: ${streak.longest} days</span>
        ${streak.last_played ? `<span class="insight-streak-last">Last: ${escapeHtml(streak.last_played)}</span>` : ''}
        <span class="insight-momentum">Last 30d: ${escapeHtml(formatHours(momentum.last_30_days_seconds))} • ${escapeHtml(deltaLabel)}</span>
      </div>
    </div>
  `;
}

export async function loadInsights() {
  const panel = $('insightsPanel');
  if (!panel) return;
  const show = AppState.appSettings ? (AppState.appSettings.show_insights !== false) : true;
  if (!show) {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = '';
  panel.innerHTML = '<div class="muted">Loading insights…</div>';
  try {
    const data = await api('/api/v2/insights/summary');
    const { heatmap, totals, top_platforms, top_genres, streak, momentum } = data;
    panel.innerHTML = `
      <div class="insights">
        ${renderTotals(totals)}
        ${renderStreak(streak, momentum)}
        <div class="insight-section">
          <h3 class="insight-title">Last year</h3>
          ${renderHeatmap(heatmap)}
          <div class="insight-legend" aria-hidden="true"><span>Less</span><span class="insight-cell level-0"></span><span class="insight-cell level-1"></span><span class="insight-cell level-2"></span><span class="insight-cell level-3"></span><span class="insight-cell level-4"></span><span>More</span></div>
        </div>
        <div class="insight-two-col">
          ${renderTopList('Top platforms', top_platforms || [], 'platform')}
          ${renderTopList('Top genres', top_genres || [], 'genre')}
        </div>
      </div>
    `;
  } catch (error) {
    panel.innerHTML = `<div class="muted">Insights unavailable: ${escapeHtml(error.message || String(error))}</div>`;
  }
}

export function bindInsights() {
  const btn = $('insightsRefresh');
  if (btn) btn.addEventListener('click', loadInsights);
  document.addEventListener('app:state-refreshed', () => {
    const panel = $('insightsPanel');
    if (panel && panel.offsetParent !== null) loadInsights();
  });
}
