/* Achievement Trail — offline RetroAchievements analytics UI (ADR 0110). */
import { escapeHtml } from './util.js';
import { api, notify, waitForJob } from './state.js';
import { t } from './i18n.js';
import { openDialog, closeDialog } from './dialogs.js';

function renderVelocityBars(weeks) {
  if (!Array.isArray(weeks) || !weeks.length) {
    return `<p class="description">${escapeHtml(t('trail.no_dates_notice'))}</p>`;
  }
  const maxCount = Math.max(1, ...weeks.map(w => Number(w.count) || 0));
  const bars = weeks.map(w => {
    const count = Number(w.count) || 0;
    const hPct = count > 0 ? Math.max(12, Math.round((count / maxCount) * 100)) : 4;
    const tip = `${w.week}: ${count} (${w.points || 0} pts)`;
    return `
      <div class="trail-bar-col" title="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}">
        <div class="trail-bar-track"><div class="trail-bar-fill${count ? '' : ' empty'}" style="height:${hPct}%"></div></div>
        <span class="trail-bar-label">${escapeHtml(String(w.week || '').slice(-3))}</span>
      </div>`;
  }).join('');
  return `<div class="trail-bars" role="img" aria-label="${escapeHtml(t('trail.velocity_chart'))}">${bars}</div>`;
}

function formatEta(item) {
  if (!item) return '—';
  if (item.eta_confidence === 'complete') return t('trail.eta_complete');
  if (typeof item.eta_days === 'number' && item.eta_days > 0) {
    return t('trail.eta_days', { days: item.eta_days, confidence: t(`trail.conf_${item.eta_confidence}`) });
  }
  const reason = item.eta_reason === 'no_dates' ? t('trail.reason_no_dates') : t('trail.reason_low_activity');
  return `— · ${reason}`;
}

function openTrailAllDialog(sortInit = 'closest') {
  const dialog = document.createElement('dialog');
  dialog.className = 'whats-new-dialog';
  dialog.innerHTML = `
    <div class="dialog-head">
      <h2>${escapeHtml(t('trail.title'))}</h2>
      <button type="button" class="icon-button" data-close aria-label="${escapeHtml(t('common.close'))}">×</button>
    </div>
    <div class="backlog-controls">
      <label class="field"><span>${escapeHtml(t('trail.sort_label'))}</span>
        <select id="trailDialogSort">
          <option value="closest">${escapeHtml(t('trail.sort_closest'))}</option>
          <option value="active">${escapeHtml(t('trail.sort_active'))}</option>
          <option value="longest">${escapeHtml(t('trail.sort_longest'))}</option>
          <option value="points">${escapeHtml(t('trail.sort_points'))}</option>
        </select>
      </label>
    </div>
    <div id="trailDialogRows" class="history-list"><p class="description">${escapeHtml(t('common.loading'))}</p></div>
    <div class="dialog-actions"><button type="button" class="primary" data-close>${escapeHtml(t('common.close'))}</button></div>
  `;
  const cleanup = () => { closeDialog(dialog); dialog.remove(); };
  dialog.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', cleanup));
  dialog.addEventListener('cancel', cleanup, { once: true });
  document.body.appendChild(dialog);
  openDialog(dialog);

  const select = dialog.querySelector('#trailDialogSort');
  const rowsHost = dialog.querySelector('#trailDialogRows');
  if (select) select.value = sortInit;
  const loadRows = async () => {
    const sort = select?.value || 'closest';
    try {
      const res = await api(`/api/v2/ra/trail?sort=${encodeURIComponent(sort)}&limit=100`);
      const items = res.items || [];
      rowsHost.innerHTML = items.length
        ? items.map(item => `
            <div class="backlog-row">
              <div class="grow">
                <strong>${escapeHtml(item.name || item.game_id)}</strong>
                ${item.platform ? `<span class="badge">${escapeHtml(item.platform)}</span>` : ''}
                <small class="description">${escapeHtml(`${item.earned}/${item.total} (${item.progress_pct}%) · ${item.earned_points} pts · ETA: ${formatEta(item)}`)}</small>
              </div>
              <button type="button" class="icon-button" data-insight-game="${escapeHtml(item.game_id)}">${escapeHtml(t('trail.open_game'))}</button>
            </div>`).join('')
        : `<p class="description">${escapeHtml(t('trail.empty'))}</p>`;
      rowsHost.querySelectorAll('[data-insight-game]').forEach(btn => {
        btn.addEventListener('click', () => {
          cleanup();
          document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: btn.dataset.insightGame } }));
        });
      });
    } catch (error) {
      rowsHost.innerHTML = `<p class="description">${escapeHtml(error.message || String(error))}</p>`;
    }
  };
  select?.addEventListener('change', loadRows);
  loadRows();
}

async function mountTrailShelf(container) {
  if (!container) return;
  let sort = 'closest';
  const load = async () => {
    try {
      const res = await api(`/api/v2/ra/trail?sort=${encodeURIComponent(sort)}&limit=5`);
      if (res.available === false) {
        container.innerHTML = `
          <div class="insight-section">
            <h3 class="insight-title">${escapeHtml(t('trail.title'))}</h3>
            <p class="description">${escapeHtml(t('trail.disabled_hint'))}</p>
          </div>`;
        return;
      }
      const s = res.summary || {};
      const items = Array.isArray(res.items) ? res.items : [];
      const nextPt = s.next_milestone?.points ?? 100;
      const nextRem = s.next_milestone?.remaining ?? 100;
      const notice = s.stale_dates_notice
        ? `<p class="description"><strong>${escapeHtml(t('trail.no_dates_notice'))}</strong></p>`
        : '';
      const rowsHtml = items.length
        ? items.map(item => `
            <div class="backlog-row">
              <div class="grow">
                <button type="button" class="link-button" data-insight-game="${escapeHtml(item.game_id)}"><strong>${escapeHtml(item.name || item.game_id)}</strong></button>
                <small class="description">${escapeHtml(`${item.earned}/${item.total} · ${formatEta(item)}`)}</small>
              </div>
              <span class="badge">${escapeHtml(`${item.progress_pct}%`)}</span>
            </div>`).join('')
        : `<p class="description">${escapeHtml(t('trail.empty'))}</p>`;

      container.innerHTML = `
        <div class="insight-section" id="trailShelfRoot">
          <div class="backlog-controls">
            <h3 class="insight-title">${escapeHtml(t('trail.title'))}</h3>
            <select id="trailSortSelect" aria-label="${escapeHtml(t('trail.sort_label'))}">
              <option value="closest">${escapeHtml(t('trail.sort_closest'))}</option>
              <option value="active">${escapeHtml(t('trail.sort_active'))}</option>
              <option value="longest">${escapeHtml(t('trail.sort_longest'))}</option>
              <option value="points">${escapeHtml(t('trail.sort_points'))}</option>
            </select>
            <button type="button" class="icon-button" id="trailRefreshBtn">${escapeHtml(t('trail.refresh'))}</button>
            <button type="button" class="icon-button" id="trailViewAllBtn">${escapeHtml(t('trail.view_all'))}</button>
          </div>
          <p class="description">${escapeHtml(t('trail.summary_line', {
            games: s.games_count || 0,
            mastered: s.mastered_games || 0,
            points: s.earned_points || 0,
            streak: s.streak_weeks || 0,
            target: nextPt,
            rem: nextRem,
          }))}</p>
          ${notice}
          ${renderVelocityBars(s.weekly_velocity)}
          <div id="trailHuntList">${rowsHtml}</div>
        </div>
      `;
      const sel = container.querySelector('#trailSortSelect');
      if (sel) {
        sel.value = sort;
        sel.addEventListener('change', () => { sort = sel.value || 'closest'; load(); });
      }
      container.querySelector('#trailViewAllBtn')?.addEventListener('click', () => openTrailAllDialog(sort));
      container.querySelector('#trailRefreshBtn')?.addEventListener('click', async btnEv => {
        const btn = btnEv.currentTarget;
        if (btn) btn.disabled = true;
        try {
          const queued = await api('/api/v2/ra/trail/refresh', { method: 'POST', body: JSON.stringify({}) });
          notify(t('trail.refresh_started'));
          await waitForJob(queued.job_id);
          notify(t('trail.refresh_done'));
          await load();
        } catch (error) {
          notify(error.message || String(error));
        } finally {
          if (btn) btn.disabled = false;
        }
      });
    } catch {
      container.innerHTML = '';
    }
  };
  await load();
}

async function mountGameTrailCard(container, gameId) {
  if (!container || !gameId) return;
  try {
    const res = await api(`/api/v2/ra/trail/game?game_id=${encodeURIComponent(gameId)}`);
    if (res.available === false || !res.game) return;
    const g = res.game;
    const nextUp = Array.isArray(g.next_up) ? g.next_up : [];
    const nextHtml = nextUp.length
      ? nextUp.slice(0, 5).map(a => `
          <div class="backlog-row">
            <span class="grow">${escapeHtml(a.title || '')}</span>
            <span class="badge">${escapeHtml(`${a.points || 0} pts`)}</span>
          </div>`).join('')
      : `<p class="description">${escapeHtml(t('trail.no_next_up'))}</p>`;

    container.innerHTML = `
      <div class="detail-card">
        <div class="backlog-controls">
          <strong>${escapeHtml(t('trail.game_card_title'))}</strong>
          <span class="badge">${escapeHtml(formatEta(g))}</span>
        </div>
        <p class="description">${escapeHtml(t('trail.next_milestone', {
          target: g.next_milestone?.points ?? 100,
          remaining: g.next_milestone?.remaining ?? 0,
        }))}</p>
        ${g.has_dates ? renderVelocityBars(g.weekly_velocity) : `<p class="description"><strong>${escapeHtml(t('trail.no_dates_notice'))}</strong></p>`}
        <h4>${escapeHtml(t('trail.next_up_title'))}</h4>
        ${nextHtml}
      </div>
    `;
  } catch {
    container.innerHTML = '';
  }
}

export { mountGameTrailCard, mountTrailShelf };
