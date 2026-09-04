/* timeline.js — session history timeline. */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { AppState, api, media } from './state.js';

function renderTimelineTab(container) {
  container.innerHTML = `<p data-i18n="common.loading">${t('common.loading')}</p>`;
  api('/api/v2/history/timeline?days=90').then(data => {
    const groups = data.groups || [];
    if (!groups.length) {
      container.innerHTML = `<p data-i18n="timeline.empty">${t('timeline.empty')}</p>`;
      return;
    }
    let html = '';
    for (const group of groups) {
      const date = group.date;
      html += `<div class="timeline-group"><h3 class="timeline-date">${escapeHtml(date)}</h3>`;
      for (const entry of group.entries) {
        const dur = Math.floor((entry.seconds || 0) / 60);
        // D5: basenames only — backend already redacts, but re-strip here so
        // a full path can never leak into the DOM even if the API regresses.
        const recBase = String(entry.recording || '').split(/[\\/]/).pop() || '';
        const rec = recBase ? `<span class="timeline-recording" data-i18n="timeline.recording" title="${escapeHtml(recBase)}">${t('timeline.recording')}</span>` : '';
        html += `
          <div class="timeline-entry" data-game-id="${escapeHtml(entry.game_id)}">
            <div class="timeline-cover" style="background-image:url('${media(entry.game_id, 'cover')}')" role="img"></div>
            <div class="timeline-meta">
              <div class="timeline-name">${escapeHtml(entry.name)}</div>
              <div class="timeline-duration">${dur}m ${rec}</div>
            </div>
          </div>
        `;
      }
      html += '</div>';
    }
    container.innerHTML = html;
    container.querySelectorAll('.timeline-entry').forEach(el => {
      el.onclick = () => {
        const gameId = el.dataset.gameId;
        if (gameId) document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId } }));
      };
    });
  }).catch(e => { container.innerHTML = `<p>${escapeHtml(e.message)}</p>`; });
}

export { renderTimelineTab };
