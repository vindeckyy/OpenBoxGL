/* Record That — the UI bridge used by recap, pause, and deeplink surfaces. */
import { api, AppState, notify } from './state.js';

function gameId(value) {
  if (value && typeof value === 'object') return String(value.game_id || value.id || '');
  return String(value || '');
}

async function captureClip(options = {}) {
  const game = options.game || AppState.games.find(item => gameId(item) === gameId(options.gameId));
  const id = gameId(options.gameId || game);
  if (!id) throw new Error('A game is required to capture a clip.');
  const result = await api('/api/v2/clips/capture', {
    method: 'POST',
    body: JSON.stringify({ game_id: id, launch_id: options.launchId || '' }),
  });
  notify(result.fallback ? 'OBS replay unavailable — screenshot captured' : 'Replay clip saved');
  document.dispatchEvent(new CustomEvent('clips:created', { detail: { ...result, game } }));
  return result;
}

async function createReel(gameOrId) {
  const game = gameOrId && typeof gameOrId === 'object'
    ? gameOrId
    : AppState.games.find(item => gameId(item) === gameId(gameOrId));
  const id = gameId(game);
  if (!id) throw new Error('A game is required to create a reel.');
  const result = await api('/api/v2/reels/create', { method: 'POST', body: JSON.stringify({ game_id: id }) });
  notify('Reel queued in Activity');
  return result;
}

function openClip(clipId) {
  const id = String(clipId || '').trim();
  if (!id) {
    notify('A clip id is required.');
    return false;
  }
  const game = AppState.games.find(item =>
    Array.isArray(item.clips) && item.clips.some(clip => String(clip?.clip_id || '') === id)
  );
  if (!game) {
    notify('That clip is no longer in the local library.');
    return false;
  }
  document.dispatchEvent(new CustomEvent('app:show-game', {
    detail: { gameId: game.game_id || game.id },
  }));
  setTimeout(() => document.getElementById('momentsTab')?.click(), 0);
  return true;
}

function registerClipHook() {
  const existing = window.OpenBoxClip || {};
  window.OpenBoxClip = { ...existing, capture: captureClip, createReel, openClip };
}

if (typeof window !== 'undefined') registerClipHook();

export { captureClip, createReel, openClip };
