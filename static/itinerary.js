/* Backlog Itinerary — dated multi-week backlog planner UI (ADR 0111). */
import { escapeHtml } from './util.js';
import { AppState, api, notify, waitForJob } from './state.js';
import { t } from './i18n.js';
import { openDialog, closeDialog, confirmAction } from './dialogs.js';
import { itineraryCapacityHours } from './pure.js';

function sourceLabel(src) {
  if (src === 'igdb') return t('itinerary.src_igdb');
  if (src === 'own_history') return t('itinerary.src_own_history');
  return t('itinerary.src_default');
}

function statusLabel(st) {
  if (st === 'done') return t('itinerary.status_done');
  if (st === 'behind') return t('itinerary.status_behind');
  if (st === 'stale') return t('itinerary.status_stale');
  return t('itinerary.status_on_track');
}

function availablePlatforms() {
  const seen = new Set();
  for (const g of AppState.games || []) {
    if (g?.platform && !g.hidden) seen.add(String(g.platform));
  }
  return [...seen].sort((a, b) => a.localeCompare(b)).slice(0, 12);
}

function renderPlanTimeline(plan) {
  const entries = Array.isArray(plan.entries) ? plan.entries : [];
  const byWeek = new Map();
  for (const entry of entries) {
    const wk = Number(entry.week) || 1;
    if (!byWeek.has(wk)) byWeek.set(wk, []);
    byWeek.get(wk).push(entry);
  }
  const weeksHtml = [...byWeek.entries()].sort((a, b) => a[0] - b[0]).map(([wk, rows]) => {
    const listHtml = rows.map(row => `
      <div class="backlog-row">
        <div class="grow">
          <button type="button" class="link-button" data-itin-game="${escapeHtml(row.game_id)}"><strong>${escapeHtml(row.name || row.game_id)}</strong></button>
          <small class="description">${escapeHtml([row.platform, `${row.est_hours}h`, sourceLabel(row.est_source), row.refined_by_ra ? t('itinerary.ra_refined') : ''].filter(Boolean).join(' · '))}</small>
        </div>
        <span class="badge">${escapeHtml(statusLabel(row.live_status))}</span>
      </div>`).join('');
    return `<div class="detail-card"><h4>${escapeHtml(t('itinerary.week_heading', { week: wk }))}</h4>${listHtml}</div>`;
  }).join('');

  const fallbackNote = plan.strategy_fallback
    ? `<p class="description"><strong>${escapeHtml(t('itinerary.fallback_notice', { used: plan.strategy_used }))}</strong></p>`
    : '';
  const laterCount = Array.isArray(plan.later) ? plan.later.length : 0;
  const laterHtml = laterCount > 0
    ? `<p class="description">${escapeHtml(t('itinerary.later_summary', { count: laterCount, hours: plan.later_hours || 0 }))}</p>`
    : '';
  const bd = plan.estimate_breakdown || {};
  return `
    <div class="detail-card">
      <p class="description">${escapeHtml(t('itinerary.plan_summary', {
        done: plan.done_count || 0,
        total: entries.length,
        pct: plan.progress_pct || 0,
        planned: plan.planned_hours || 0,
        cap: plan.capacity_hours || 0,
      }))}</p>
      <p class="description">${escapeHtml(t('itinerary.coverage_line', {
        igdb: bd.igdb || 0,
        history: bd.own_history || 0,
        fallback: bd.default || 0,
      }))}</p>
      ${fallbackNote}
      <div class="extras">
        <button type="button" class="primary" id="itineraryReplanBtn">${escapeHtml(t('itinerary.replan'))}</button>
        <button type="button" class="icon-button" id="itineraryNewBtn">${escapeHtml(t('itinerary.new_plan'))}</button>
        <button type="button" class="icon-button" id="itineraryAbandonBtn">${escapeHtml(t('itinerary.abandon'))}</button>
      </div>
    </div>
    ${weeksHtml || `<p class="description">${escapeHtml(t('itinerary.empty'))}</p>`}
    ${laterHtml}
  `;
}

function renderWizard(existing = null) {
  const plats = availablePlatforms();
  const horizon = existing?.horizon_days ?? 30;
  const pace = existing?.minutes_per_day ?? 60;
  const strategy = existing?.strategy ?? 'quick-wins';
  const platBoxes = plats.length
    ? `<fieldset class="field"><legend>${escapeHtml(t('itinerary.platforms_label'))}</legend><div class="extras">${plats.map(p => `<label class="check"><input type="checkbox" name="platform" value="${escapeHtml(p)}"> <span>${escapeHtml(p)}</span></label>`).join('')}</div></fieldset>`
    : '';

  return `
    <form class="form" id="itineraryWizardForm">
      <p class="description">${escapeHtml(t('itinerary.wizard_intro'))}</p>
      <label class="field">
        <span>${escapeHtml(t('itinerary.horizon_label'))}</span>
        <input name="horizon_days" type="number" min="7" max="365" value="${escapeHtml(String(horizon))}" required>
      </label>
      <label class="field">
        <span>${escapeHtml(t('itinerary.pace_label'))}</span>
        <input name="minutes_per_day" type="number" min="5" max="960" value="${escapeHtml(String(pace))}" required>
      </label>
      <p class="description" id="itineraryCapacityPreview">${escapeHtml(t('itinerary.capacity_preview', { hours: itineraryCapacityHours(horizon, pace) }))}</p>
      <label class="field">
        <span>${escapeHtml(t('itinerary.strategy_label'))}</span>
        <select name="strategy">
          <option value="quick-wins"${strategy === 'quick-wins' ? ' selected' : ''}>${escapeHtml(t('itinerary.strategy_quick_wins'))}</option>
          <option value="comeback"${strategy === 'comeback' ? ' selected' : ''}>${escapeHtml(t('itinerary.strategy_comeback'))}</option>
          <option value="raided"${strategy === 'raided' ? ' selected' : ''}>${escapeHtml(t('itinerary.strategy_raided'))}</option>
        </select>
      </label>
      ${platBoxes}
      <label class="check"><input type="checkbox" name="fetch_igdb"> <span>${escapeHtml(t('itinerary.fetch_igdb_label'))}</span></label>
      <div class="dialog-actions">
        ${existing ? `<button type="button" class="icon-button" id="itineraryCancelWizard">${escapeHtml(t('common.cancel'))}</button>` : ''}
        <button type="submit" class="primary">${escapeHtml(t('itinerary.build_btn'))}</button>
      </div>
    </form>
  `;
}

async function openItineraryDialog() {
  const dialog = document.createElement('dialog');
  dialog.className = 'whats-new-dialog';
  dialog.innerHTML = `
    <div class="dialog-head">
      <h2>${escapeHtml(t('itinerary.menu_title'))}</h2>
      <button type="button" class="icon-button" data-close aria-label="${escapeHtml(t('common.close'))}">×</button>
    </div>
    <div id="itineraryDialogBody"><p class="description">${escapeHtml(t('common.loading'))}</p></div>
    <div class="dialog-actions"><button type="button" class="primary" data-close>${escapeHtml(t('common.close'))}</button></div>
  `;
  const cleanup = () => { closeDialog(dialog); dialog.remove(); };
  dialog.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', cleanup));
  dialog.addEventListener('cancel', cleanup, { once: true });
  document.body.appendChild(dialog);
  openDialog(dialog);

  const body = dialog.querySelector('#itineraryDialogBody');
  let showWizard = false;
  let lastPlan = null;

  const renderBody = async () => {
    try {
      const res = await api('/api/v2/itinerary');
      lastPlan = res.plan || null;
      if (!lastPlan || showWizard) {
        body.innerHTML = renderWizard(lastPlan);
        const form = body.querySelector('#itineraryWizardForm');
        const preview = body.querySelector('#itineraryCapacityPreview');
        const updateCap = () => {
          if (!form || !preview) return;
          const cap = itineraryCapacityHours(form.horizon_days.value, form.minutes_per_day.value);
          preview.textContent = t('itinerary.capacity_preview', { hours: cap });
        };
        form?.horizon_days?.addEventListener('input', updateCap);
        form?.minutes_per_day?.addEventListener('input', updateCap);
        body.querySelector('#itineraryCancelWizard')?.addEventListener('click', () => {
          showWizard = false;
          renderBody();
        });
        form?.addEventListener('submit', async ev => {
          ev.preventDefault();
          const f = ev.currentTarget;
          const platforms = [...f.querySelectorAll('input[name="platform"]:checked')].map(i => i.value);
          const payload = {
            horizon_days: Number(f.horizon_days.value),
            minutes_per_day: Number(f.minutes_per_day.value),
            strategy: f.strategy.value,
            platforms,
            fetch_igdb: Boolean(f.fetch_igdb?.checked),
          };
          try {
            const out = await api('/api/v2/itinerary/plan', { method: 'POST', body: JSON.stringify(payload) });
            if (out.job_id) {
              notify(t('itinerary.fetching_igdb'));
              await waitForJob(out.job_id);
            }
            notify(t('itinerary.created_notice'));
            showWizard = false;
            await renderBody();
          } catch (error) {
            notify(error.message || String(error));
          }
        });
        return;
      }

      body.innerHTML = renderPlanTimeline(lastPlan);
      body.querySelector('#itineraryNewBtn')?.addEventListener('click', () => {
        showWizard = true;
        renderBody();
      });
      body.querySelector('#itineraryReplanBtn')?.addEventListener('click', async () => {
        try {
          const out = await api('/api/v2/itinerary/replan', { method: 'POST', body: JSON.stringify({}) });
          if (out.dropped_ids?.length) {
            notify(t('itinerary.replanned_dropped', { count: out.dropped_ids.length }));
          } else {
            notify(t('itinerary.replanned_ok'));
          }
          await renderBody();
        } catch (error) {
          notify(error.message || String(error));
        }
      });
      body.querySelector('#itineraryAbandonBtn')?.addEventListener('click', async () => {
        const ok = await confirmAction({
          title: t('itinerary.abandon_title'),
          message: t('itinerary.abandon_message'),
          confirmLabel: t('itinerary.abandon'),
          destructive: true,
        });
        if (!ok) return;
        await api('/api/v2/itinerary/abandon', { method: 'POST', body: JSON.stringify({}) });
        notify(t('itinerary.abandoned_notice'));
        showWizard = true;
        await renderBody();
      });
      body.querySelectorAll('[data-itin-game]').forEach(btn => {
        btn.addEventListener('click', () => {
          cleanup();
          document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: btn.dataset.itinGame } }));
        });
      });
    } catch (error) {
      body.innerHTML = `<p class="description">${escapeHtml(error.message || String(error))}</p>`;
    }
  };
  await renderBody();
}

async function mountItineraryWidget(container) {
  if (!container) return;
  try {
    const res = await api('/api/v2/itinerary');
    const plan = res.plan;
    if (!plan) {
      container.innerHTML = '';
      return;
    }
    const entries = Array.isArray(plan.entries) ? plan.entries : [];
    container.innerHTML = `
      <div class="insight-section" id="itineraryInsightsWidget">
        <div class="backlog-controls">
          <h3 class="insight-title">${escapeHtml(t('itinerary.menu_title'))}</h3>
          <button type="button" class="icon-button" id="openItineraryFromInsights">${escapeHtml(t('itinerary.open_btn'))}</button>
        </div>
        <p class="description">${escapeHtml(t('itinerary.plan_summary', {
          done: plan.done_count || 0,
          total: entries.length,
          pct: plan.progress_pct || 0,
          planned: plan.planned_hours || 0,
          cap: plan.capacity_hours || 0,
        }))}</p>
      </div>
    `;
    container.querySelector('#openItineraryFromInsights')?.addEventListener('click', openItineraryDialog);
  } catch {
    container.innerHTML = '';
  }
}

export { mountItineraryWidget, openItineraryDialog };
