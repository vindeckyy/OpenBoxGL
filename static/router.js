/* Hash router — 1.8.0 (ADR 0021)
   Encodes the library view state in the URL hash so a refresh (or a shared
   link) restores the context: platform, category, playlist, filter preset,
   search query, selected game, and sort. replaceState-only: no history spam;
   the hash is written on every render and applied on boot/hashchange.
   The grid/list toggle stays a setting (not routed) on purpose.
*/
import { $ } from './util.js';
import { AppState, invalidateFilterCache } from './state.js';

const ROUTE_KEYS = ['platform', 'category', 'playlist', 'preset', 'q', 'game', 'sort'];

function encodeSegment(value) {
  return encodeURIComponent(String(value)).replace(/[!'()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

function decodeSegment(value) {
  try { return decodeURIComponent(value); } catch { return value; }
}

function hashSegments() {
  return location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
}

/** Build the hash string for the current view state. */
function currentHash() {
  const parts = [];
  const push = (key, value) => {
    if (value !== undefined && value !== null && String(value) !== '') parts.push(`${key}/${encodeSegment(value)}`);
  };
  push('platform', AppState.platform && AppState.platform !== 'all' ? AppState.platform : '');
  push('category', AppState.platformCategory && AppState.platformCategory !== 'all' ? AppState.platformCategory : '');
  push('playlist', AppState.activePlaylist);
  push('preset', AppState.activeFilterPreset);
  push('q', $('sidebarSearch')?.value?.trim());
  push('game', AppState.selectedId);
  push('sort', $('sort')?.value && $('sort').value !== 'title' ? $('sort').value : '');
  return `#/${parts.join('/')}`;
}

/** Write the current view state into the hash (no history entry). */
export function syncHash() {
  if (typeof history === 'undefined' || typeof location === 'undefined') return;
  const next = currentHash();
  if (location.hash === next) return;
  const base = `${location.pathname}${location.search}`;
  history.replaceState(null, '', next === '#/' ? base : base + next);
}

/** Apply the hash to AppState and the toolbar inputs. Returns true when something changed. */
export function applyHash() {
  const segments = hashSegments();
  if (!segments.length) return false;
  let changed = false;
  const pair = (key, apply) => {
    const index = segments.indexOf(key);
    if (index >= 0 && index + 1 < segments.length) apply(decodeSegment(segments[index + 1]));
  };
  pair('platform', value => { if (AppState.platform !== value) { changed = true; AppState.platform = value; } });
  pair('category', value => { if ((AppState.platformCategory || 'all') !== value) { changed = true; AppState.platformCategory = value; } });
  pair('playlist', value => { if (AppState.activePlaylist !== value) { changed = true; AppState.activePlaylist = value; } });
  pair('preset', value => { if (AppState.activeFilterPreset !== value) { changed = true; AppState.activeFilterPreset = value; } });
  pair('q', value => {
    const search = $('sidebarSearch');
    if (search && search.value.trim() !== value) { changed = true; search.value = value; invalidateFilterCache(); }
  });
  pair('game', value => {
    const id = Number(value);
    if (Number.isFinite(id) && AppState.selectedId !== id) { changed = true; AppState.selectedId = id; }
  });
  pair('sort', value => {
    const sort = $('sort');
    if (sort && sort.value !== value && sort.querySelector(`option[value="${CSS.escape(value)}"]`)) {
      changed = true;
      sort.value = value;
    }
  });
  return changed;
}

export { ROUTE_KEYS };
