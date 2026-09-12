/* timemachine.js — journal timeline and read-only as-of browser (T2). */
import { $, escapeHtml } from './util.js';
import { api, notify } from './state.js';
import { t } from './i18n.js';
import { confirmAction } from './dialogs.js';

let bound = false;
let eventOffset = 0;
let eventRows = [];
let loadingEvents = false;

function dialog() {
  return $('timeMachineDialog');
}

function valueText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(item => String(item)).join(', ');
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return String(value); }
  }
  return String(value);
}

function kindText(kind) {
  const key = String(kind || '').replace(/[^a-z]/g, '');
  return t(`time_machine.kind_${key || 'edit'}`);
}

function renderChanges(changes) {
  if (!changes || typeof changes !== 'object' || !Object.keys(changes).length) {
    return `<span class="muted">${escapeHtml(t('time_machine.no_changes'))}</span>`;
  }
  return Object.entries(changes).map(([field, values]) => {
    const from = values && typeof values === 'object' ? values.from : null;
    const to = values && typeof values === 'object' ? values.to : null;
    return `<span class="tm-change"><strong>${escapeHtml(field)}</strong>: ${escapeHtml(valueText(from))} → ${escapeHtml(valueText(to))}</span>`;
  }).join('');
}

function renderWarnings(result) {
  const warnings = [];
  if (result.horizon) warnings.push(`${escapeHtml(t('time_machine.truncated', { date: result.horizon }))}`);
  if (result.gap) warnings.push(escapeHtml(t('time_machine.gap')));
  if (result.corrupt?.length) warnings.push(escapeHtml(t('time_machine.corrupt')));
  return warnings.length ? `<div class="description tm-warnings" role="status">${warnings.join(' ')}</div>` : '';
}

function renderEvents(result) {
  const body = $('timeMachineEventsBody');
  if (!body) return;
  const events = result.events || [];
  if (!eventRows.length) {
    body.innerHTML = `${renderWarnings(result)}<p class="muted">${escapeHtml(t('time_machine.empty'))}</p>`;
    return;
  }
  const groups = new Map();
  events.forEach(event => {
    const day = String(event.created_at || '').slice(0, 10) || '—';
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(event);
  });
  let html = renderWarnings(result);
  groups.forEach((rows, day) => {
    html += `<div class="timeline-group"><h3 class="timeline-date">${escapeHtml(day)}</h3>`;
    rows.forEach(event => {
      const name = event.name || event.game_id || event.sync_key || t('common.none');
      const changes = event.changes || {};
      const fields = Object.keys(changes);
      html += `
        <article class="timeline-entry tm-event" data-event-id="${escapeHtml(event.event_id)}">
          <div class="timeline-meta">
            <div class="timeline-name">${escapeHtml(kindText(event.kind))} · ${escapeHtml(name)}</div>
            <div class="timeline-duration">${escapeHtml(String(event.created_at || ''))} · ${escapeHtml(t('time_machine.event_id'))}: ${escapeHtml(event.event_id)}</div>
            <div class="tm-changes">${renderChanges(changes)}</div>
          </div>
          <button type="button" class="icon-button tm-revert" data-tm-revert="${escapeHtml(event.event_id)}" data-tm-name="${escapeHtml(name)}" data-tm-fields="${escapeHtml(JSON.stringify(fields))}">${escapeHtml(t('time_machine.revert'))}</button>
        </article>`;
    });
    html += '</div>';
  });
  if (result.has_more) {
    html += `<div class="dialog-actions"><button type="button" class="icon-button" id="tmLoadMore">${escapeHtml(t('time_machine.load_more'))}</button></div>`;
  }
  body.innerHTML = html;
  body.querySelectorAll('[data-tm-revert]').forEach(button => {
    button.onclick = () => previewRevert(button);
  });
  $('tmLoadMore')?.addEventListener('click', () => loadEvents(false), { once: true });
}

function renderAsOf(result) {
  const body = $('timeMachineAsOfBody');
  const meta = $('timeMachineAsOfMeta');
  if (!body || !meta) return;
  const warnings = [];
  if (result.before_first_event) warnings.push(t('time_machine.before_first'));
  if (result.truncated) warnings.push(t('time_machine.truncated', { date: result.horizon || '—' }));
  if (result.corrupt?.length) warnings.push(t('time_machine.corrupt'));
  meta.textContent = `${result.count || 0} ${t('time_machine.games')}${warnings.length ? ` · ${warnings.join(' ')}` : ''}`;
  const games = result.games || [];
  if (!games.length) {
    body.innerHTML = `<p class="muted">${escapeHtml(t('time_machine.no_games'))}</p>`;
    return;
  }
  body.innerHTML = `<div class="platforms tm-asof-grid">${games.map(game => {
    const id = game.game_id || game.sync_key || '';
    return `<button type="button" class="platform" data-tm-game="${escapeHtml(id)}"><span>${escapeHtml(game.name || id)}</span><small>${escapeHtml(game.platform || '')} · ${escapeHtml(game.genre || '')}</small></button>`;
  }).join('')}</div>`;
  body.querySelectorAll('[data-tm-game]').forEach(button => {
    button.onclick = () => {
      document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: button.dataset.tmGame } }));
      dialog()?.close();
    };
  });
}

function eventQuery(offset) {
  const params = new URLSearchParams({ offset: String(offset), limit: '200' });
  const days = $('tmDays')?.value?.trim();
  const kind = $('tmKind')?.value || '';
  const game = $('tmGame')?.value?.trim();
  if (days) params.set('days', days);
  if (kind) params.set('kind', kind);
  if (game) params.set('game_id', game);
  return params;
}

async function loadEvents(reset = true) {
  if (loadingEvents) return;
  const body = $('timeMachineEventsBody');
  if (!body) return;
  if (reset) {
    eventOffset = 0;
    eventRows = [];
    body.innerHTML = `<p class="muted">${escapeHtml(t('time_machine.loading'))}</p>`;
  }
  loadingEvents = true;
  try {
    const result = await api(`/api/v2/library/time-machine/events?${eventQuery(eventOffset)}`);
    eventRows = reset ? (result.events || []) : [...eventRows, ...(result.events || [])];
    eventOffset = eventRows.length;
    renderEvents({ ...result, events: eventRows });
  } catch (error) {
    body.innerHTML = `<p class="muted">${escapeHtml(error.message || String(error))}</p>`;
  } finally {
    loadingEvents = false;
  }
}

async function loadAsOf() {
  const body = $('timeMachineAsOfBody');
  const rawDate = $('tmAsOfDate')?.value?.trim();
  if (!body) return;
  if (!rawDate) {
    body.innerHTML = `<p class="muted">${escapeHtml(t('time_machine.no_date'))}</p>`;
    return;
  }
  body.innerHTML = `<p class="muted">${escapeHtml(t('time_machine.loading'))}</p>`;
  try {
    const params = new URLSearchParams({ date: rawDate });
    const result = await api(`/api/v2/library/time-machine/as-of?${params}`);
    renderAsOf(result);
  } catch (error) {
    body.innerHTML = `<p class="muted">${escapeHtml(error.message || String(error))}</p>`;
  }
}

async function previewRevert(button) {
  let fields = [];
  try { fields = JSON.parse(button.dataset.tmFields || '[]'); } catch { fields = []; }
  try {
    const preview = await api('/api/v2/library/time-machine/revert', {
      method: 'POST',
      body: JSON.stringify({ event_id: button.dataset.tmRevert, fields }),
    });
    const plan = preview.plan || {};
    if (!await confirmAction({
      title: t('time_machine.revert_title'),
      message: t('time_machine.revert_message'),
      target: t('time_machine.revert_target', { event: button.dataset.tmRevert }),
      consequence: t('time_machine.revert_consequence'),
      retained: t('time_machine.revert_retained'),
      recovery: t('time_machine.revert_recovery'),
      confirmLabel: t('time_machine.revert_confirm'),
    })) return;
    const result = await api('/api/v2/library/time-machine/revert', {
      method: 'POST',
      body: JSON.stringify({
        event_id: button.dataset.tmRevert,
        fields: plan.fields,
        undo: plan.undo,
        base_token: plan.base_token,
        apply: true,
      }),
    });
    notifyRevert(result);
    await loadEvents(true);
  } catch (error) {
    notifyRevertError(error);
  }
}

function notifyRevert(result) {
  const message = result?.changed
    ? t('time_machine.reverted', { count: result.applied || 0 })
    : t('time_machine.no_changes');
  notify(message);
}

function notifyRevertError(error) {
  notify(error.message || String(error));
}

function showTab(tab) {
  const events = tab === 'events';
  $('timeMachineEventsPane').hidden = !events;
  $('timeMachineAsOfPane').hidden = events;
  $('tmEventsTab').setAttribute('aria-selected', String(events));
  $('tmAsOfTab').setAttribute('aria-selected', String(!events));
  $('tmEventsTab').classList.toggle('active', events);
  $('tmAsOfTab').classList.toggle('active', !events);
  if (!events && !$('timeMachineAsOfBody').dataset.loaded) loadAsOf();
}

function bindTimeMachine() {
  if (bound) return;
  const target = dialog();
  if (!target) return;
  bound = true;
  $('closeTimeMachine').onclick = () => target.close();
  $('tmEventsTab').onclick = () => showTab('events');
  $('tmAsOfTab').onclick = () => showTab('asof');
  $('tmRefresh').onclick = () => loadEvents(true);
  $('tmKind').onchange = () => loadEvents(true);
  $('tmDays').onchange = () => loadEvents(true);
  $('tmGame').onkeydown = event => { if (event.key === 'Enter') loadEvents(true); };
  $('tmAsOfDate').onchange = () => { $('timeMachineAsOfBody').dataset.loaded = ''; };
  $('tmAsOfButton').onclick = async () => {
    $('timeMachineAsOfBody').dataset.loaded = '1';
    await loadAsOf();
  };
  target.addEventListener('close', () => { $('timeMachineAsOfBody').dataset.loaded = ''; });
  document.addEventListener('localechange', () => {
    if (target.open) {
      showTab($('timeMachineEventsPane').hidden ? 'asof' : 'events');
      if (!$('timeMachineEventsPane').hidden) loadEvents(true);
    }
  });
}

function openTimeMachine() {
  bindTimeMachine();
  const target = dialog();
  if (!target) return;
  showTab('events');
  if (!target.open) target.showModal();
  loadEvents(true);
}

export { openTimeMachine, loadEvents, loadAsOf };
