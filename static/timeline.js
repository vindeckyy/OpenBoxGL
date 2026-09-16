/* timeline.js — session history timeline. */
import { $, escapeHtml } from './util.js';
import { t, onLocaleChange } from './i18n.js';
import { api, media } from './state.js';

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
        const rec = entry.recording ? `<span class="timeline-recording" data-i18n="timeline.recording">${t('timeline.recording')}</span>` : '';
        // P7: entries are real buttons so Enter/Space activate them natively;
        // the cover image gets its own label instead of a bare role="img".
        html += `
          <button type="button" class="timeline-entry" data-game-id="${escapeHtml(entry.game_id)}" aria-label="${escapeHtml(t('timeline.open_game', {name: entry.name}))}">
            <div class="timeline-cover" style="background-image:url('${media(entry.game_id, 'cover')}')" role="img" aria-label="${escapeHtml(t('timeline.cover_label', {name: entry.name}))}"></div>
            <div class="timeline-meta">
              <div class="timeline-name">${escapeHtml(entry.name)}</div>
              <div class="timeline-duration">${dur}m ${rec}</div>
            </div>
          </button>
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

// P8: re-render the visible timeline after a language change instead of
// leaving the entries in the previous language until reload.
onLocaleChange(() => {
  const container = $('timelineContainer');
  if (container && container.offsetParent !== null && container.querySelector('.timeline-entry')) {
    renderTimelineTab(container);
  }
});

export { renderTimelineTab };
