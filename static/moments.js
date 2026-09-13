/* Moments timeline — T1-ui
   The module owns capture affordances, timeline rendering, deterministic share
   payloads, and the M-key/automatic-capture bridge.  It intentionally keeps
   dialogs and library rendering as dynamic/event-driven dependencies so the
   existing navigation modules remain the first writers for their hotkeys.
*/
import { $, escapeHtml } from './util.js';
import { AppState, api, media, notify, token } from './state.js';
import { t } from './i18n.js';

const MILESTONE_SECONDS = [3600, 5 * 3600, 10 * 3600, 25 * 3600, 50 * 3600, 100 * 3600];
const autoCaptureInFlight = new Set();
let previousSnapshots = null;

function stableGameId(game) {
  return String(game?.game_id || game?.stable_game_id || game?.id || '');
}

function resolveGame(gameOrId) {
  if (gameOrId && typeof gameOrId === 'object') {
    if (gameOrId.game && typeof gameOrId.game === 'object') return gameOrId.game;
    return gameOrId;
  }
  const id = String(gameOrId ?? '');
  return AppState.games.find(game => String(game.game_id || '') === id || String(game.id) === id) || null;
}

function snapshot(game) {
  return {
    play_count: Number(game?.play_count || 0),
    progress: Number(game?.progress || 0),
    playtime_seconds: Number(game?.playtime_seconds || 0),
    ra_achievements_earned: Number(game?.ra_achievements_earned || game?.achievements_earned || 0),
  };
}

function autoMomentTrigger(previous, current) {
  const before = snapshot(previous);
  const after = snapshot(current);
  if (before.play_count < 1 && after.play_count >= 1) return 'first_boot';
  if (after.ra_achievements_earned > before.ra_achievements_earned) return 'ra_unlock';
  if (before.progress < 1 && after.progress >= 1) return 'progress';
  if (MILESTONE_SECONDS.some(mark => before.playtime_seconds < mark && after.playtime_seconds >= mark)) return 'milestone';
  return '';
}

function formatDate(value) {
  if (!value) return t('moments.date_unknown');
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
  } catch {
    return String(value).replace('T', ' ');
  }
}

function triggerLabel(trigger) {
  const key = `moments.trigger_${String(trigger || 'manual').replace(/[^a-z0-9_]/gi, '')}`;
  return t(key);
}

function screenshotUrl(game, item) {
  const index = Number(item?.screenshot_index);
  return Number.isInteger(index) && index >= 0 ? media(game, 'screenshot', index) : '';
}

function clipUrl(game, item, index) {
  if (!item?.path) return '';
  return `/api/media?game_id=${encodeURIComponent(stableGameId(game))}&kind=clip&index=${encodeURIComponent(index)}&v=${encodeURIComponent(String(AppState.mediaEpoch))}&token=${encodeURIComponent(token)}`;
}

function memoryMediaUrl(game, index) {
  const normalizedIndex = Number(index);
  const gameId = stableGameId(game);
  if (!gameId || !Number.isInteger(normalizedIndex) || normalizedIndex < 0) return '';
  return `/api/v2/memories/media?game_id=${encodeURIComponent(gameId)}&index=${encodeURIComponent(normalizedIndex)}&v=${encodeURIComponent(String(AppState.mediaEpoch))}&token=${encodeURIComponent(token)}`;
}

function shareCardPayload(moment, game) {
  const item = moment || {};
  return {
    version: 1,
    moment_id: String(item.moment_id || ''),
    game_id: stableGameId(game) || String(item.game_id || ''),
    game_name: String(game?.name || ''),
    platform: String(game?.platform || ''),
    title: String(item.title || t('moments.tab')),
    note: String(item.note || ''),
    trigger: String(item.trigger || 'manual'),
    created_at: String(item.created_at || ''),
    screenshot_index: Number.isInteger(Number(item.screenshot_index)) ? Number(item.screenshot_index) : null,
    has_resume: Boolean(item.resume_state),
    capture_id: String(item.resume_state?.capture_id || ''),
    resume_state: item.resume_state,
  };
}

function cssToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function wrapCanvasText(ctx, text, maxWidth) {
  const words = String(text || '').split(/\s+/).filter(Boolean);
  const lines = [];
  let line = '';
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(candidate).width > maxWidth) {
      lines.push(line);
      line = word;
    } else line = candidate;
  }
  if (line) lines.push(line);
  return lines.slice(0, 5);
}

function loadCanvasImage(url) {
  return new Promise(resolve => {
    if (!url) return resolve(null);
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = url;
  });
}

async function buildShareCard(moment, game) {
  const canvas = document.createElement('canvas');
  canvas.width = 1200;
  canvas.height = 630;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error(t('moments.share_unavailable'));
  const surface = cssToken('--moments-share-bg') || cssToken('--surface-card');
  const text = cssToken('--text');
  const muted = cssToken('--moments-share-muted') || cssToken('--muted');
  const accent = cssToken('--moments-share-accent') || cssToken('--brand');
  ctx.fillStyle = surface;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const image = await loadCanvasImage(screenshotUrl(game, moment));
  const imageWidth = image ? 420 : 0;
  if (image) {
    const scale = Math.max(imageWidth / image.width, canvas.height / image.height);
    const width = image.width * scale;
    const height = image.height * scale;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, imageWidth, canvas.height);
    ctx.clip();
    ctx.drawImage(image, (imageWidth - width) / 2, (canvas.height - height) / 2, width, height);
    ctx.restore();
  }
  const left = image ? imageWidth + 64 : 64;
  const max = canvas.width - left - 64;
  const family = getComputedStyle(document.body).fontFamily || 'sans-serif';
  ctx.fillStyle = accent;
  ctx.font = `600 20px ${family}`;
  ctx.fillText(triggerLabel(moment?.trigger), left, 82);
  ctx.fillStyle = text;
  ctx.font = `700 48px ${family}`;
  const titleLines = wrapCanvasText(ctx, game?.name || t('moments.tab'), max);
  titleLines.forEach((line, index) => ctx.fillText(line, left, 148 + index * 56));
  ctx.fillStyle = muted;
  ctx.font = `500 22px ${family}`;
  ctx.fillText(`${moment?.title || t('moments.tab')} · ${formatDate(moment?.created_at)}`, left, 148 + titleLines.length * 56 + 18);
  ctx.fillStyle = text;
  ctx.font = `400 26px ${family}`;
  const noteLines = wrapCanvasText(ctx, moment?.note || t('moments.empty_hint'), max);
  const noteTop = 232 + titleLines.length * 56;
  noteLines.forEach((line, index) => ctx.fillText(line, left, noteTop + index * 38));
  ctx.fillStyle = muted;
  ctx.font = `400 18px ${family}`;
  ctx.fillText(game?.platform || '', left, canvas.height - 44);
  return canvas;
}

function canvasBlob(canvas) {
  return new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
}

async function shareMoment(moment, game) {
  try {
    const canvas = await buildShareCard(moment, game);
    const blob = await canvasBlob(canvas);
    if (blob && navigator.clipboard?.write && typeof globalThis.ClipboardItem !== 'undefined') {
      await navigator.clipboard.write([new globalThis.ClipboardItem({ 'image/png': blob })]);
      notify(t('moments.share_copied'));
      return true;
    }
    throw new Error('clipboard unavailable');
  } catch {
    try {
      const link = document.createElement('a');
      link.download = `openbox-moment-${String(moment?.moment_id || 'card')}.png`;
      link.href = (await buildShareCard(moment, game)).toDataURL('image/png');
      link.click();
      notify(t('moments.share_downloaded'));
      return true;
    } catch (error) {
      notify(error.message || t('moments.share_unavailable'));
      return false;
    }
  }
}

async function promptMomentNote(defaultValue = '') {
  const { promptInput } = await import('./dialogs.js');
  return promptInput({
    title: t('moments.note'),
    message: t('moments.note_prompt'),
    label: t('moments.note'),
    defaultValue,
  });
}

async function captureMoment(gameOrOptions, options = {}) {
  let source = gameOrOptions;
  let settings = { ...options };
  if (gameOrOptions && typeof gameOrOptions === 'object' && (gameOrOptions.game || gameOrOptions.gameId || gameOrOptions.launchId || gameOrOptions.trigger)) {
    settings = { ...gameOrOptions, ...options };
    source = settings.game || settings.gameId;
  }
  const game = resolveGame(source);
  if (!game) {
    notify(t('moments.game_missing'));
    return null;
  }
  let note = settings.note;
  if (note === undefined && !settings.silent) note = await promptMomentNote('');
  if (note === null) return null;
  const body = {
    game_id: stableGameId(game),
    note: String(note || ''),
    trigger: settings.trigger || 'manual',
    capture: settings.capture !== false,
    launch_id: settings.launchId || settings.launch_id || '',
  };
  if (settings.title) body.title = settings.title;
  if (settings.includeResume !== false) {
    try {
      const status = await api(`/api/v2/resume/status?game_id=${encodeURIComponent(stableGameId(game))}`);
    if (status.enabled && status.capable && status.available && !status.stale && status.state) body.resume_state = status.state;
    } catch { /* resume is optional for a moment */ }
  }
  try {
    const result = await api('/api/v2/moments', { method: 'POST', body: JSON.stringify(body) });
    const item = result.item || result;
    document.dispatchEvent(new CustomEvent('moments:created', { detail: { item, game } }));
    if (!settings.silent) notify(item.screenshot ? t('moments.captured') : t('moments.saved_note'));
    return item;
  } catch (error) {
    if (!settings.silent) notify(error.message || t('moments.capture_failed'));
    return null;
  }
}

async function captureMomentInteractive(gameOrOptions, options = {}) {
  let game = gameOrOptions;
  let settings = { ...options };
  if (gameOrOptions && typeof gameOrOptions === 'object' && (gameOrOptions.game || gameOrOptions.gameId || gameOrOptions.launchId || gameOrOptions.trigger)) {
    settings = { ...gameOrOptions, ...options };
    game = settings.game || settings.gameId;
  }
  const note = await promptMomentNote(settings.note || '');
  if (note === null) return null;
  return captureMoment(game, { ...settings, note, silent: true }).then(item => {
    if (item) notify(item.screenshot ? t('moments.captured') : t('moments.saved_note'));
    return item;
  });
}

function canResumeItem(item, status) {
  return Boolean(item?.resume_state?.immutable && item.resume_state.sha256 && item.resume_state.file && status?.enabled && status?.capable && !status?.stale);
}

function renderGalleryState(titleKey, hintKey = '') {
  const hint = hintKey ? `<span>${escapeHtml(t(hintKey))}</span>` : '';
  return `<div class="moments-empty"><strong>${escapeHtml(t(titleKey))}</strong>${hint}</div>`;
}

function renderMomentItems(items, game, resumeStatus) {
  if (!items.length) return `<div class="moments-empty"><strong>${escapeHtml(t('moments.empty'))}</strong><span>${escapeHtml(t('moments.empty_hint'))}</span></div>`;
  return items.map(item => {
    const image = screenshotUrl(game, item);
    const resume = canResumeItem(item, resumeStatus);
    return `<article class="moment-card" data-moment-id="${escapeHtml(item.moment_id)}">
      <div class="moment-thumb">${image ? `<img src="${image}" alt="" loading="lazy" decoding="async">` : `<span>${escapeHtml(t('moments.note_only'))}</span>`}</div>
      <div class="moment-card-body"><div class="moment-card-head"><strong>${escapeHtml(item.title || t('moments.tab'))}</strong><time datetime="${escapeHtml(item.created_at)}">${escapeHtml(formatDate(item.created_at))}</time></div>
      <span class="moment-trigger">${escapeHtml(triggerLabel(item.trigger))}</span>${item.note ? `<p>${escapeHtml(item.note)}</p>` : ''}${item.capture_error ? `<p class="moment-warning">${escapeHtml(t('moments.capture_failed'))}</p>` : ''}
      <div class="moment-actions"><button type="button" class="icon-button" data-moment-action="edit">${escapeHtml(t('moments.edit_note'))}</button><button type="button" class="icon-button" data-moment-action="share">${escapeHtml(t('moments.share'))}</button>${resume ? `<button type="button" class="icon-button" data-moment-action="resume">${escapeHtml(t('moments.resume'))}</button>` : ''}<button type="button" class="icon-button" data-moment-action="delete">${escapeHtml(t('moments.delete'))}</button></div></div>
    </article>`;
  }).join('');
}

function renderClipItems(items, game, error = null) {
  if (error && !items.length) return renderGalleryState('moments.clips_error');
  if (!items.length) return renderGalleryState('moments.clips_empty', 'moments.clips_empty_hint');
  return items.map((item, index) => {
    const title = item.title || (item.fallback ? t('moments.screenshot_fallback') : t('moments.replay_clip'));
    const source = item.fallback ? t('moments.screenshot_fallback') : t('moments.replay_clip');
    return `<article class="moment-card clip-card"><div class="moment-thumb"><video controls preload="metadata" src="${escapeHtml(clipUrl(game, item, index))}"></video></div><div class="moment-card-body"><div class="moment-card-head"><strong>${escapeHtml(title)}</strong><time datetime="${escapeHtml(item.created_at || '')}">${escapeHtml(formatDate(item.created_at))}</time></div><span class="moment-trigger">${escapeHtml(source)}</span></div></article>`;
  }).join('');
}

function renderMemoryItems(items, game, error = null) {
  if (error) return renderGalleryState('moments.memory_error', 'moments.memory_error_hint');
  if (!items.length) return renderGalleryState('moments.memory_empty', 'moments.memory_empty_hint');
  return items.map((item, index) => {
    const image = memoryMediaUrl(game, index);
    const date = item?.taken_at || item?.imported_at || '';
    return `<article class="moment-card memory-card"><div class="moment-thumb">${image ? `<img data-memory-media src="${escapeHtml(image)}" alt="${escapeHtml(t('moments.memory_alt'))}" loading="lazy" decoding="async">` : `<span>${escapeHtml(t('moments.memory_media_error'))}</span>`}</div><div class="moment-card-body"><div class="moment-card-head"><strong>${escapeHtml(t('moments.memory_item'))}</strong><time datetime="${escapeHtml(date)}">${escapeHtml(formatDate(date))}</time></div></div></article>`;
  }).join('');
}

function attachMemoryMediaFallback(host) {
  host.querySelectorAll('[data-memory-media]').forEach(image => image.addEventListener('error', () => {
    const fallback = document.createElement('span');
    fallback.textContent = t('moments.memory_media_error');
    image.replaceWith(fallback);
  }, { once: true }));
}

async function mountMomentsPanel(gameOrId, host) {
  const game = resolveGame(gameOrId);
  if (!game || !host) return;
  const gameId = stableGameId(game);
  host.dataset.momentsGameId = gameId;
  host.innerHTML = `<div class="moments-toolbar"><span class="description">${escapeHtml(t('moments.loading'))}</span></div>`;
  let result;
  let clipsResult;
  let memoriesResult;
  let status = null;
  try {
    [result, clipsResult, status, memoriesResult] = await Promise.all([
      api(`/api/v2/moments?game_id=${encodeURIComponent(gameId)}`),
      api(`/api/v2/clips?game_id=${encodeURIComponent(gameId)}`)
        .then(payload => ({ clips: Array.isArray(payload?.clips) ? payload.clips : [], error: null }))
        .catch(error => ({ clips: Array.isArray(game.clips) ? game.clips : [], error })),
      api(`/api/v2/resume/status?game_id=${encodeURIComponent(gameId)}`).catch(() => null),
      api(`/api/v2/memories?game_id=${encodeURIComponent(gameId)}`)
        .then(payload => ({ memories: Array.isArray(payload?.memories) ? payload.memories : [], error: null }))
        .catch(error => ({ memories: [], error })),
    ]);
  } catch {
    host.innerHTML = renderGalleryState('moments.panel_error');
    return;
  }
  if (host.dataset.momentsGameId !== gameId) return;
  const items = Array.isArray(result?.items) ? result.items : [];
  const clips = Array.isArray(clipsResult?.clips) ? clipsResult.clips : [];
  const memories = Array.isArray(memoriesResult?.memories) ? memoriesResult.memories : [];
  const reelAction = (items.length || clips.length) && window.OpenBoxClip?.createReel
    ? `<button type="button" class="icon-button" id="createReelPanel">${escapeHtml(t('moments.create_reel'))}</button>`
    : '';
  host.innerHTML = `<div class="moments-toolbar"><span class="moment-count">${escapeHtml(t('moments.count', { count: result?.total ?? items.length }))}</span><button type="button" class="primary" id="captureMomentPanel">${escapeHtml(t('moments.capture'))}</button>${reelAction}</div><div class="moments-list"><h3>${escapeHtml(t('moments.tab'))}</h3>${renderMomentItems(items, game, status)}<h3>${escapeHtml(t('moments.clips'))}</h3>${renderClipItems(clips, game, clipsResult?.error)}<h3>${escapeHtml(t('moments.memory_gallery'))}</h3>${renderMemoryItems(memories, game, memoriesResult?.error)}</div>`;
  attachMemoryMediaFallback(host);
  $('createReelPanel')?.addEventListener('click', async () => {
    try { await window.OpenBoxClip.createReel(game); } catch { notify(t('moments.panel_error')); }
  });
  $('captureMomentPanel')?.addEventListener('click', async () => {
    await captureMomentInteractive(game, { trigger: 'manual' });
    await mountMomentsPanel(game, host);
  });
  host.querySelectorAll('[data-moment-action]').forEach(button => button.addEventListener('click', async () => {
    const card = button.closest('[data-moment-id]');
    const item = items.find(candidate => candidate.moment_id === card?.dataset.momentId);
    if (!item) return;
    const action = button.dataset.momentAction;
    try {
      if (action === 'edit') {
        const note = await promptMomentNote(item.note || '');
        if (note === null) return;
        await api('/api/v2/moments/update', { method: 'POST', body: JSON.stringify({ game_id: gameId, moment_id: item.moment_id, note }) });
      } else if (action === 'delete') {
        const { confirmAction } = await import('./dialogs.js');
        const ok = await confirmAction({ title: t('moments.delete_title'), message: t('moments.delete_consequence'), target: item.title, recovery: t('moments.delete_recovery'), confirmLabel: t('moments.delete'), destructive: true });
        if (!ok) return;
        await api('/api/v2/moments/delete', { method: 'POST', body: JSON.stringify({ game_id: gameId, moment_id: item.moment_id }) });
      } else if (action === 'share') {
        await shareMoment(item, game);
        return;
      } else if (action === 'resume') {
        await api('/api/v2/moments/resume', { method: 'POST', body: JSON.stringify({ game_id: gameId, moment_id: item.moment_id }) });
        notify(t('moments.resume_started'));
        return;
      }
      await mountMomentsPanel(game, host);
    } catch (error) {
      notify(error.message || t('moments.capture_failed'));
    }
  }));
}

async function mountResumeAffordance(gameOrId, host) {
  const game = resolveGame(gameOrId);
  if (!game || !host) return;
  host.innerHTML = '';
  // Games without a server-issued stable id (synthetic client-side entries)
  // cannot resolve server-side; the status probe would always 400.
  if (!game.game_id && !game.stable_game_id) return;
  try {
    const status = await api(`/api/v2/resume/status?game_id=${encodeURIComponent(stableGameId(game))}`);
    if (!status.enabled || !status.capable || !status.available || status.stale) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'icon-button resume-action';
    button.textContent = t('moments.resume_latest');
    button.title = t('moments.resume_latest');
    button.onclick = async () => {
      try {
        await api('/api/v2/resume', { method: 'POST', body: JSON.stringify({ game_id: stableGameId(game) }) });
        notify(t('moments.resume_started'));
      } catch (error) { notify(error.message || t('moments.resume_unavailable')); }
    };
    host.appendChild(button);
  } catch { /* unsupported adapters have no affordance */ }
}

async function openMoment(momentId) {
  const id = String(momentId || '').trim();
  if (!id) {
    document.getElementById('momentsTab')?.click();
    return;
  }
  try {
    const result = await api(`/api/v2/moments?moment_id=${encodeURIComponent(id)}`);
    const gameId = String(result.game_id || result.item?.game_id || '');
    if (!gameId) throw new Error(t('moments.game_missing'));
    document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId } }));
    setTimeout(() => document.getElementById('momentsTab')?.click(), 0);
  } catch (error) { notify(error.message || t('moments.capture_failed')); }
}

document.addEventListener('app:state-refreshed', () => {
  const current = new Map(AppState.games.map(game => [stableGameId(game), snapshot(game)]));
  if (!previousSnapshots) {
    previousSnapshots = current;
    return;
  }
  const enabled = Boolean(AppState.appSettings.moments_autocapture);
  for (const game of AppState.games) {
    const gameId = stableGameId(game);
    const trigger = autoMomentTrigger(previousSnapshots.get(gameId), game);
    if (!enabled || !trigger || autoCaptureInFlight.has(gameId)) continue;
    autoCaptureInFlight.add(gameId);
    captureMoment(game, { trigger, note: t(`moments.auto_${trigger}`), silent: true })
      .finally(() => autoCaptureInFlight.delete(gameId));
  }
  previousSnapshots = current;
});

window.OpenBoxMoments = { captureMoment, captureMomentInteractive, openMoment, shareCardPayload };

export {
  autoMomentTrigger,
  captureMoment,
  captureMomentInteractive,
  mountMomentsPanel,
  mountResumeAffordance,
  openMoment,
  shareCardPayload,
};
