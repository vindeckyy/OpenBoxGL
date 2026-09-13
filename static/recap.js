/* Session recap card (S2).
   Renders the recap payload emitted as the "session.recap" SSE event after
   finish_session commits, or fetched from GET /api/v2/sessions/recap (the
   ?deeplink=recap consumer and the SSE-drop recovery path both go through
   openSessionRecap/showSessionRecapForStopped). Moment/clip capture buttons
   stay hidden until their owning lanes ship a surface: T1-ui's moments.js
   exporting captureMoment(), or T3-obs registering window.OpenBoxClip. */
import { $, escapeHtml, duration } from './util.js';
import { api, notify, AppState } from './state.js';
import { t } from './i18n.js';
import { updateGameStatus } from './library.js';

const PROGRESS_ACTIONS = [
  { value: 'Playing', label: 'dialog.progress_playing' },
  { value: 'Beaten', label: 'dialog.progress_beaten' },
  { value: 'Completed', label: 'dialog.progress_completed' },
  { value: 'Mastered', label: 'dialog.progress_mastered' },
];
let _lastShownLaunchId = '';
let _captureHooks = null;

function recapEnabled() {
  return !AppState.appSettings || AppState.appSettings.session_recap_enabled !== false;
}

function _esc(value) {
  return escapeHtml(String(value ?? ''));
}

function _findGame(payload) {
  return AppState.games.find(item => item.game_id === payload.game_id) || null;
}

async function _captureSupport() {
  // Resolve once per page load; absent surfaces keep their buttons hidden.
  if (_captureHooks) return _captureHooks;
  const hooks = { moment: null, clip: null };
  try {
    const mod = await import('./moments.js');
    if (mod && typeof mod.captureMoment === 'function') hooks.moment = mod.captureMoment;
  } catch { /* moments.js not shipped yet */ }
  const clip = window.OpenBoxClip;
  if (clip && typeof clip.capture === 'function') hooks.clip = clip.capture.bind(clip);
  _captureHooks = hooks;
  return hooks;
}

function _stat(label, value) {
  return `<div class="recap-stat"><small>${_esc(label)}</small><span>${_esc(value)}</span></div>`;
}

function _renderStats(payload) {
  const stats = [
    _stat(t('recap.duration'), duration(payload.seconds)),
    _stat(t('recap.started'), String(payload.started_at || '').replace('T', ' ') || '—'),
    _stat(t('recap.exit_code'), payload.exit_code === 0 ? t('recap.exit_clean') : String(payload.exit_code)),
    _stat(t('recap.screenshots'), String(payload.screenshots_taken ?? 0)),
  ];
  const ra = payload.ra || {};
  if (ra.tracked && ra.available) {
    const delta = ra.earned_delta ? ` (+${ra.earned_delta})` : '';
    stats.push(_stat(t('recap.achievements'), `${ra.earned}/${ra.total}${delta}${ra.mastered ? ' · ' + t('recap.mastered') : ''}`));
  }
  if (payload.resume_state_available) {
    stats.push(_stat(t('recap.resume'), t('recap.resume_ready')));
  }
  return `<div class="recap-stats">${stats.join('')}</div>`;
}

function _renderProgressActions(game) {
  if (!game) return '';
  const buttons = PROGRESS_ACTIONS.map(action =>
    `<button type="button" class="icon-button recap-progress${game.progress === action.value ? ' active' : ''}" data-recap-progress="${_esc(action.value)}">${_esc(t(action.label))}</button>`
  ).join('');
  return `<div class="recap-section"><small class="recap-label">${_esc(t('recap.set_progress'))}</small><div class="recap-actions">${buttons}</div></div>`;
}

function _renderCaptureActions(payload, hooks) {
  const buttons = [];
  if (hooks.moment || payload?.capture?.moment) {
    buttons.push(`<button type="button" class="icon-button" data-recap-capture="moment" ${hooks.moment ? '' : 'disabled'}>${_esc(t('recap.moment'))}</button>`);
  }
  if (hooks.clip || payload?.capture?.clip) {
    buttons.push(`<button type="button" class="icon-button" data-recap-capture="clip" ${hooks.clip ? '' : 'disabled'}>${_esc(t('recap.clip'))}</button>`);
  }
  if (!buttons.length) return '';
  return `<div class="recap-section"><div class="recap-actions">${buttons.join('')}</div></div>`;
}

function _bindActions(payload, hooks) {
  const card = $('recapCard');
  if (!card) return;
  card.querySelectorAll('[data-recap-progress]').forEach(button => {
    button.onclick = () => {
      const game = _findGame(payload);
      if (!game) return;
      updateGameStatus(game.id, button.dataset.recapProgress);
    };
  });
  card.querySelectorAll('[data-recap-capture]').forEach(button => {
    button.onclick = () => {
      const kind = button.dataset.recapCapture;
      const hook = hooks[kind];
      if (hook) {
        try {
          hook({ gameId: payload.game_id, launchId: payload.launch_id, game: _findGame(payload) });
        } catch { notify(t('recap.capture_failed')); }
      }
    };
  });
}

async function showSessionRecap(payload) {
  if (!payload || typeof payload !== 'object' || !recapEnabled()) return;
  const launchId = String(payload.launch_id || '');
  if (launchId && launchId === _lastShownLaunchId) return;
  if (launchId) _lastShownLaunchId = launchId;
  const card = $('recapCard');
  const dialog = $('recapDialog');
  if (!card || !dialog) return;
  const hooks = await _captureSupport();
  const name = _esc(payload.name || '—');
  const badges = [
    payload.partial ? `<span class="recap-badge">${_esc(t('recap.partial'))}</span>` : '',
    payload.game_missing ? `<span class="recap-badge">${_esc(t('recap.missing_game'))}</span>` : '',
  ].filter(Boolean).join('');
  card.innerHTML = `<div class="recap-head"><strong class="recap-game">${name}</strong>${badges}</div>`
    + _renderStats(payload)
    + _renderProgressActions(_findGame(payload))
    + _renderCaptureActions(payload, hooks);
  _bindActions(payload, hooks);
  const close = $('closeRecap');
  if (close) close.onclick = () => dialog.close();
  const done = $('recapDone');
  if (done) done.onclick = () => dialog.close();
  if (!dialog.open) dialog.showModal();
}

async function showSessionRecapForStopped() {
  // Polling path: the stopped event fired but the recap SSE may have dropped —
  // the route always serves the newest committed recap.
  if (!recapEnabled()) return;
  try {
    const payload = await api('/api/v2/sessions/recap');
    await showSessionRecap(payload);
  } catch { /* no recap stored — nothing to show */ }
}

async function openSessionRecap() {
  try {
    const payload = await api('/api/v2/sessions/recap');
    await showSessionRecap(payload);
  } catch { notify(t('recap.none')); }
}

export { showSessionRecap, showSessionRecapForStopped, openSessionRecap };
