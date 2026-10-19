// Theme sidecar audio. A theme may ship <theme>.json next to its CSS with
// relative music/sounds paths; the server serves those through
// /api/theme-asset. Missing sidecars are silent, never an error.
import { api, token } from './state.js';

const state = {
  name: '',
  music: '',
  sounds: {},
  musicEl: null,
  enabled: true,
  loading: null,
};

function assetUrl(relative) {
  if (!relative) return '';
  return `/api/theme-asset?name=${encodeURIComponent(state.name)}&path=${encodeURIComponent(relative)}&token=${encodeURIComponent(token)}`;
}

export async function configureThemeSounds(name, { enabled = true } = {}) {
  const theme = String(name || '').trim();
  state.enabled = enabled !== false;
  if (!state.enabled) return stopThemeMusic();
  if (theme === state.name && state.loading) return state.loading;
  stopThemeMusic();
  state.name = theme;
  state.music = '';
  state.sounds = {};
  if (!theme) return;
  state.loading = api(`/api/theme-sounds?name=${encodeURIComponent(theme)}`)
    .then(result => {
      if (state.name !== theme) return;
      state.music = result?.music || '';
      state.sounds = result?.sounds && typeof result.sounds === 'object' ? result.sounds : {};
    })
    .catch(() => {})
    .finally(() => { state.loading = null; });
  return state.loading;
}

export function startThemeMusic() {
  if (!state.enabled || !state.music) return;
  if (!state.musicEl) {
    state.musicEl = new Audio();
    state.musicEl.loop = true;
    state.musicEl.volume = 0.35;
    state.musicEl.preload = 'none';
  }
  const source = assetUrl(state.music);
  if (state.musicEl.src !== new URL(source, location.href).href) state.musicEl.src = source;
  state.musicEl.play().catch(() => {});
}

export function stopThemeMusic() {
  if (!state.musicEl) return;
  state.musicEl.pause();
}

export function playThemeSound(kind) {
  if (!state.enabled) return;
  const relative = state.sounds[kind];
  if (!relative) return;
  const audio = new Audio(assetUrl(relative));
  audio.volume = 0.5;
  audio.play().catch(() => {});
}
