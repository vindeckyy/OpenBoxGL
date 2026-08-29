import { $, escapeHtml, duration, fact, RATIO_BUCKETS, RATIO_REP, coverBucketOf, artworkKinds, trigramsOf, expandTrigrams } from './util.js';
import { token, AppState, selectedIds, media, badgeVisibility, renderBadges, api, nativePickFolder, nativeReveal, nativeOpenExternal, notify, setButtonBusy, ensureProfiles, applyLocaleStrings, applySidebarVisibility, platformCategoryFor, filteredGames, warmSearchIndex, loadExplorerFacets, scheduleSearch, resetQuery, invalidateFilterCache } from './state.js';
import { loadTheme, deletePlaylist } from './settings.js';
import { importFolder, importSteam, importHeroic, importLutris, importDroppedFolder } from './imports.js';
import { openGameDialog, confirmAction, promptInput } from './dialogs.js';
import { openMetadata, steamMetadata, loadAchievements } from './metadata.js';
import { captureScreenshot, downloadBezel } from './media.js';
import { launch, backupSaves, discoverSaves, loadBackups } from './sessions.js';
import { installGameyfin, uninstallGameyfin, ludusaviAction, hoardAction } from './storefront.js';
import { openReader } from './reader.js';

const DETAILS_WIDTH_KEY = 'openbox-details-width';
const DETAILS_COLLAPSED_KEY = 'openbox-details-collapsed';
const VIRTUAL_GRID_KEY = 'openbox-virtual-grid';
let lastFacetsFingerprint = null;
let detailsDragActive = false;
let detailSheetScrollTop = 0;

// Virtual grid feature flag: localStorage['openbox-virtual-grid'] !== '0' enables windowing.
// When disabled, grid renders all items without spacers or IntersectionObserver.
function isVirtualEnabled() {
  try { return localStorage.getItem(VIRTUAL_GRID_KEY) !== '0'; } catch { return true; }
}

// Trigram Worker: offload trigram expansion/search when Worker is available, fallback to main thread otherwise.
// Shared trigram logic lives in util.js (trigramsOf/expandTrigrams) and worker.search.js for parity.
let _searchWorker = null;
let _workerSeq = 0;
const _workerPending = new Map();
function getSearchWorker() {
  if (_searchWorker) return _searchWorker;
  try {
    if (typeof Worker !== 'function') return null;
    const w = new Worker('./static/worker.search.js');
    w.onmessage = e => {
      const data = e.data || {};
      const pending = _workerPending.get(data.id);
      if (pending) {
        _workerPending.delete(data.id);
        if (data.error) pending.reject(new Error(data.error));
        else pending.resolve(data);
      }
    };
    w.onerror = e => {
      // Worker failed: clear pending with fallback
      for (const [, pending] of _workerPending) pending.reject(new Error(e.message || 'Worker error'));
      _workerPending.clear();
    };
    _searchWorker = w;
    return w;
  } catch { return null; }
}
function workerSearch(query, games) {
  const w = getSearchWorker();
  if (!w) return null;
  const id = String(++_workerSeq);
  return new Promise((resolve, reject) => {
    _workerPending.set(id, { resolve, reject });
    try { w.postMessage({ id, type: 'search', query, games }); } catch (err) { _workerPending.delete(id); reject(err); }
    // fallback timeout: if worker doesn't respond in 200ms, reject to fallback
    setTimeout(() => {
      if (_workerPending.has(id)) { _workerPending.delete(id); reject(new Error('Worker timeout')); }
    }, 200);
  });
}
async function searchWithFallback(query, games) {
  // Try worker first, fallback to main-thread advancedQueryMatches + trigramScore
  try {
    const res = await workerSearch(query, games);
    if (res && Array.isArray(res.results)) return res.results;
  } catch { /* fallback */ }
  // Main-thread fallback: identical logic to worker.search.js searchGames
  const q = String(query || '').trim().toLowerCase();
  if (!q) return games.slice();
  const qTrigrams = trigramsOf(q);
  return games.filter(game => {
    const hay = [game.name, game.sort_title, ...(Array.isArray(game.alternate_names) ? game.alternate_names : [])].filter(Boolean).join(' ').toLowerCase();
    if (hay.includes(q)) return true;
    if (qTrigrams.size >= 3) {
      const hayTri = trigramsOf(hay);
      let common = 0;
      for (const t of qTrigrams) if (hayTri.has(t)) common++;
      if (common / qTrigrams.size >= 0.5) return true;
    }
    if (q.length >= 2 && q.length <= 8 && /^[a-z0-9]+$/i.test(q)) {
      const words = String(game.name || '').trim().match(/[A-Za-z0-9]+/g) || [];
      const acronym = words.map(w => w[0].toLowerCase()).join('');
      if (acronym === q || acronym.includes(q)) return true;
      if (words.length > 1 && ['the', 'a', 'an'].includes(words[0].toLowerCase())) {
        const sub = words.slice(1).map(w => w[0].toLowerCase()).join('');
        if (sub === q || sub.includes(q)) return true;
      }
    }
    return false;
  });
}
// Expose for testing that worker and main produce identical results (F1 acceptance)
async function verifyWorkerParity(query, games) {
  const main = await searchWithFallback(query, games);
  try {
    const w = getSearchWorker();
    if (!w) return { parity: true, reason: 'no-worker-fallback' };
    const id = String(++_workerSeq);
    const workerRes = await new Promise((resolve, reject) => {
      _workerPending.set(id, { resolve, reject });
      w.postMessage({ id, type: 'search', query, games });
      setTimeout(() => { if (_workerPending.has(id)) { _workerPending.delete(id); reject(new Error('timeout')); } }, 500);
    });
    const wResults = workerRes.results || [];
    const same = main.length === wResults.length && main.every((g, i) => g.id === wResults[i].id);
    return { parity: same, main: main.length, worker: wResults.length };
  } catch (e) { return { parity: false, error: String(e.message || e) }; }
}

// IntersectionObserver for virtual spacer windowing
let _virtualObserver = null;
function ensureVirtualObserver() {
  if (!isVirtualEnabled()) return;
  if (typeof IntersectionObserver === 'undefined') return;
  const pane = document.querySelector('main.library');
  if (!pane) return;
  if (_virtualObserver) _virtualObserver.disconnect();
  _virtualObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting && entry.intersectionRatio > 0) {
        // Spacer became visible: expand window by rendering with updated scroll
        const top = pane.scrollTop;
        if (gridRowHeight && Math.abs(top - gridScrollTop) < 1) return;
        gridScrollTop = top;
        renderGrid({ fromScroll: true });
      }
    }
  }, { root: pane, threshold: 0, rootMargin: '400px' });
  // Observe current spacers after next render
  requestAnimationFrame(() => {
    const spacers = document.querySelectorAll('#grid .grid-spacer');
    spacers.forEach(el => _virtualObserver.observe(el));
  });
}

function leaveActivePreset() {
  if (AppState.activeFilterPreset) AppState.activeFilterPreset = '';
}
function ensureSmartFilterRules() {
  if (!AppState.smartFilterRules) AppState.smartFilterRules = {};
  return AppState.smartFilterRules;
}
function visibleGames() {
  const rules = AppState.smartFilterRules || {};
  let games = filteredGames();
  if (rules.has_achievements) games = games.filter(game => game.has_achievements);
  if (rules.has_missing_media) games = games.filter(game => game.has_missing_media);
  if (rules.has_highscores) games = games.filter(game => game.has_highscores);
  return games;
}
function openManualReader(game) {
  if (!game?.has_manual) return;
  openReader({...game, documents: [{name: 'Manual', path: game.manual || 'manual'}]}, 0, media(game, 'manual'));
}
function currentPresetRules() {
  const preset = AppState.filterPresets.find(item => item.name === AppState.activeFilterPreset);
  return preset?.rules || {};
}
function effectiveQueryState() {
  const presetRules = currentPresetRules();
  return {
    view: presetRules.view || $('view')?.value || 'all',
    platform: presetRules.platform || AppState.platform,
    platformCategory: presetRules.platform_category || AppState.platformCategory,
    esrb: presetRules.esrb || $('esrbFilter')?.value || '',
    query: (presetRules.query || $('sidebarSearch')?.value || '').trim(),
    playlist: AppState.activePlaylist,
    preset: AppState.activeFilterPreset,
    explorer: {...AppState.explorerRules},
    importBatchId: AppState.importBatchId,
    smart: {...(AppState.smartFilterRules || {})},
  };
}
function applyDetailsLayout() {
  const workspace = document.querySelector('.workspace');
  const details = $('details');
  if (!workspace || !details) return;
  const narrow = window.matchMedia('(max-width:760px)').matches;
  const collapsed = localStorage.getItem(DETAILS_COLLAPSED_KEY) === '1';
  const width = Math.min(640, Math.max(280, Number(localStorage.getItem(DETAILS_WIDTH_KEY)) || 410));
  details.classList.toggle('details-collapsed', collapsed && !narrow);
  if (narrow) {
    workspace.style.gridTemplateColumns = '';
    details.style.width = '';
    details.classList.toggle('detail-sheet-open', AppState.selectedId !== null);
    if (AppState.selectedId !== null) {
      details.style.position = 'fixed';
      details.style.left = '0';
      details.style.right = '0';
      details.style.bottom = '0';
      details.style.maxHeight = '72vh';
      details.style.zIndex = '8';
      details.style.borderTop = '1px solid var(--line)';
    } else {
      details.style.position = '';
      details.style.left = '';
      details.style.right = '';
      details.style.bottom = '';
      details.style.maxHeight = '';
      details.style.zIndex = '';
      details.style.borderTop = '';
    }
    return;
  }
  details.classList.remove('detail-sheet-open');
  details.style.position = '';
  details.style.left = '';
  details.style.right = '';
  details.style.bottom = '';
  details.style.maxHeight = '';
  details.style.zIndex = '';
  details.style.borderTop = '';
  if (collapsed) {
    workspace.style.gridTemplateColumns = '190px minmax(520px,1fr) 0';
    details.style.width = '0';
    details.style.overflow = 'hidden';
  } else {
    workspace.style.gridTemplateColumns = `190px minmax(520px,1fr) ${width}px`;
    details.style.width = `${width}px`;
    details.style.overflow = '';
  }
}
function bindDetailsResize() {
  const handle = $('detailsResizeHandle');
  if (!handle || handle.dataset.bound) return;
  handle.dataset.bound = '1';
  const onMove = event => {
    if (!detailsDragActive) return;
    const workspace = document.querySelector('.workspace');
    if (!workspace) return;
    const width = Math.min(640, Math.max(280, workspace.getBoundingClientRect().right - event.clientX));
    localStorage.setItem(DETAILS_WIDTH_KEY, String(width));
    applyDetailsLayout();
  };
  const stopDrag = () => {
    if (!detailsDragActive) return;
    detailsDragActive = false;
    document.body.style.userSelect = '';
  };
  handle.addEventListener('mousedown', event => {
    if (window.matchMedia('(max-width:760px)').matches) return;
    detailsDragActive = true;
    document.body.style.userSelect = 'none';
    event.preventDefault();
  });
  handle.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const delta = event.key === 'ArrowLeft' ? 24 : -24;
    const width = Math.min(640, Math.max(280, Number(localStorage.getItem(DETAILS_WIDTH_KEY) || 410) + delta));
    localStorage.setItem(DETAILS_WIDTH_KEY, String(width));
    applyDetailsLayout();
  });
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', stopDrag);
  handle.addEventListener('dblclick', () => {
    const collapsed = localStorage.getItem(DETAILS_COLLAPSED_KEY) === '1';
    localStorage.setItem(DETAILS_COLLAPSED_KEY, collapsed ? '0' : '1');
    applyDetailsLayout();
  });
}
function bindFilterDrawer() {
  const drawer = $('filterDrawer');
  const body = $('filterDrawerBody');
  const openBtn = $('filterDrawerButton');
  if (!drawer || !body || body.dataset.bound) return;
  body.dataset.bound = '1';
  body.innerHTML = `<div class="platforms smart-filter-grid">${[
    ['favorite', 'Favorites'],
    ['installed', 'Installed'],
    ['progress', 'In progress'],
    ['save', 'Has saves'],
    ['achievement', 'Achievements'],
    ['missing-media', 'Missing media'],
    ['high-score', 'High scores'],
  ].map(([key, label]) => `<button type="button" class="platform" data-smart-filter="${key}">${escapeHtml(label)}</button>`).join('')}</div>
    <label class="field"><span>Developer contains</span><input id="smartFilterDeveloper" type="search" placeholder="Developer"></label>
    <label class="field"><span>Publisher contains</span><input id="smartFilterPublisher" type="search" placeholder="Publisher"></label>
    <div class="extras"><button type="button" class="primary" id="applySmartFilters">Apply smart filters</button></div>
    <details class="advanced-search-help"><summary>Advanced search help</summary><p class="description">Use typed tokens in search: <code>developer:"Name"</code>, <code>publisher:"Name"</code>, <code>favorite:yes</code>, <code>installed:yes</code>, <code>progress:Playing</code>, <code>import_batch_id:"id"</code>. Prefix with <code>-</code> to exclude.</p></details>`;
  body.querySelectorAll('[data-smart-filter]').forEach(button => {
    const key = button.dataset.smartFilter;
    const actions = {
      favorite: () => { $('view').value = 'favorites'; },
      installed: () => { $('view').value = 'installed'; },
      progress: () => { $('view').value = 'playing'; },
      save: () => { $('view').value = 'saves'; },
      achievement: () => { ensureSmartFilterRules().has_achievements = true; },
      'missing-media': () => { ensureSmartFilterRules().has_missing_media = true; },
      'high-score': () => { ensureSmartFilterRules().has_highscores = true; },
    };
    button.onclick = () => {
      leaveActivePreset();
      actions[key]?.();
      render();
      drawer.close();
    };
  });
  $('applySmartFilters').onclick = () => {
    leaveActivePreset();
    const dev = $('smartFilterDeveloper')?.value.trim();
    const pub = $('smartFilterPublisher')?.value.trim();
    const parts = [];
    if (dev) parts.push(`developer:"${dev.replace(/"/g, '')}"`);
    if (pub) parts.push(`publisher:"${pub.replace(/"/g, '')}"`);
    if (parts.length) $('sidebarSearch').value = parts.join(' ');
    drawer.close();
    render();
  };
  if (openBtn) openBtn.onclick = () => drawer.showModal();
  if ($('closeFilterDrawer')) $('closeFilterDrawer').onclick = () => drawer.close();
}
async function updateActivePreset() {
  const name = AppState.activeFilterPreset;
  if (!name) return;
  const preset = AppState.filterPresets.find(item => item.name === name);
  const rules = {
    platform: AppState.platform,
    platform_category: AppState.platformCategory,
    view: $('view').value,
    query: $('sidebarSearch').value.trim(),
    esrb: $('esrbFilter')?.value || '',
    progress: AppState.explorerRules.progress || ($('view').value === 'playing' ? 'Playing' : ''),
  };
  try {
    await api('/api/filter-presets', {method: 'POST', body: JSON.stringify({name, rules, bigbox_quick: preset?.bigbox_quick || false})});
    await refresh();
    notify('Filter preset updated');
  } catch (error) { notify(error.message); }
}
function renderQueryChips() {
  const container = $('queryChips');
  if (!container) return;
  const state = effectiveQueryState();
  const chips = [];
  const addChip = (key, label, remove) => chips.push(`<button type="button" class="platform query-chip" data-chip-remove="${escapeHtml(key)}" aria-label="Remove ${escapeHtml(label)}">${escapeHtml(label)} ×</button>`);
  if (state.preset) addChip('preset', `Preset: ${state.preset}`, () => { AppState.activeFilterPreset = ''; });
  if (state.playlist) addChip('playlist', `Playlist: ${state.playlist}`, () => { AppState.activePlaylist = ''; });
  if (state.platform !== 'all') addChip('platform', `Platform: ${state.platform}`, () => { AppState.platform = 'all'; });
  if (state.platformCategory !== 'all') addChip('category', `Category: ${state.platformCategory}`, () => { AppState.platformCategory = 'all'; });
  if (state.view !== 'all') addChip('view', `View: ${$('view').selectedOptions[0]?.text || state.view}`, () => { $('view').value = 'all'; });
  if (state.esrb) addChip('esrb', `ESRB: ${state.esrb}`, () => { if ($('esrbFilter')) $('esrbFilter').value = ''; });
  if (state.query) addChip('search', `Search: ${state.query}`, () => { $('sidebarSearch').value = ''; invalidateFilterCache(); });
  if (state.explorer.progress) addChip('explorer', `Progress: ${state.explorer.progress === '__unset' ? 'Unset' : state.explorer.progress}`, () => { AppState.explorerRules = {}; });
  if (state.importBatchId) addChip('import_batch', `Import batch: ${state.importBatchId}`, () => { AppState.importBatchId = ''; invalidateFilterCache(); });
  if (state.smart.has_achievements) addChip('smart_achievements', 'Achievements', () => { delete AppState.smartFilterRules.has_achievements; });
  if (state.smart.has_missing_media) addChip('smart_missing_media', 'Missing media', () => { delete AppState.smartFilterRules.has_missing_media; });
  if (state.smart.has_highscores) addChip('smart_highscores', 'High scores', () => { delete AppState.smartFilterRules.has_highscores; });
  const hasFilters = chips.length > 0;
  const actions = [];
  if (state.preset) actions.push('<button type="button" class="platform query-chip" id="updateActivePreset">Update preset</button>');
  if (hasFilters) actions.push('<button type="button" class="platform query-chip" data-chip="clear-all">Clear all</button>');
  container.innerHTML = actions.join('') + chips.join('');
  container.querySelector('[data-chip="clear-all"]')?.addEventListener('click', () => {
    AppState.smartFilterRules = {};
    resetQuery();
    render();
  });
  if ($('updateActivePreset')) $('updateActivePreset').onclick = () => updateActivePreset();
  container.querySelectorAll('[data-chip-remove]').forEach(button => {
    button.onclick = () => {
      leaveActivePreset();
      const key = button.dataset.chipRemove;
      if (key === 'preset') AppState.activeFilterPreset = '';
      else if (key === 'playlist') AppState.activePlaylist = '';
      else if (key === 'platform') AppState.platform = 'all';
      else if (key === 'category') AppState.platformCategory = 'all';
      else if (key === 'view') $('view').value = 'all';
      else if (key === 'esrb' && $('esrbFilter')) $('esrbFilter').value = '';
      else if (key === 'search') { $('sidebarSearch').value = ''; invalidateFilterCache(); }
      else if (key === 'explorer') AppState.explorerRules = {};
      else if (key === 'import_batch') { AppState.importBatchId = ''; invalidateFilterCache(); }
      else if (key === 'smart_achievements') delete AppState.smartFilterRules.has_achievements;
      else if (key === 'smart_missing_media') delete AppState.smartFilterRules.has_missing_media;
      else if (key === 'smart_highscores') delete AppState.smartFilterRules.has_highscores;
      render();
    };
  });
}
function markFilterAria() {
  document.querySelectorAll('#platformCategories [data-platform-category]').forEach(button => {
    const active = button.classList.contains('active');
    button.setAttribute('aria-current', active ? 'true' : 'false');
  });
  document.querySelectorAll('#platforms [data-platform]').forEach(button => {
    const active = button.classList.contains('active');
    button.setAttribute('aria-current', active ? 'true' : 'false');
  });
  document.querySelectorAll('[data-playlist]').forEach(button => {
    const active = button.classList.contains('active');
    button.setAttribute('aria-current', active ? 'true' : 'false');
  });
  document.querySelectorAll('[data-filter-preset]').forEach(button => {
    const active = button.classList.contains('active');
    button.setAttribute('aria-current', active ? 'true' : 'false');
  });
  document.querySelectorAll('[data-game]').forEach(card => {
    const id = Number(card.dataset.game);
    const selected = AppState.selectedId === id || selectedIds.has(id);
    card.setAttribute('aria-selected', selected ? 'true' : 'false');
  });
}

    async function refresh() {
      const state = await api('/api/library');
      AppState.games = state.games;
      AppState.playlists = state.playlists || [];
      AppState.filterPresets = state.filter_presets || state.settings?.filter_presets || AppState.appSettings.filter_presets || [];
      AppState.raConfigured = state.ra_configured;
      AppState.appSettings = state.settings || AppState.appSettings;
      AppState.mediaEpoch = state.media_epoch || 0;
      AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
      if (AppState.activePlaylist && !AppState.playlists.some(item => item.name === AppState.activePlaylist)) AppState.activePlaylist = '';
      if (AppState.selectedId !== null && !AppState.games.some(game => game.id === AppState.selectedId)) AppState.selectedId = null;
      for (const id of selectedIds) if (!AppState.games.some(game => game.id === id)) selectedIds.delete(id);
      render();
      applySidebarVisibility();
      applyLocaleStrings();
      if (!AppState.appSettings.welcome_completed && !AppState.games.length) $('setupCenter').showModal();
      setTimeout(() => { try { warmSearchIndex(); } catch(error) { AppState.searchIndexError = error.message; } }, 0);
      const fingerprint = `${AppState.games.length}:${AppState.games[0]?.id || ''}:${AppState.games.at(-1)?.id || ''}`;
      if (lastFacetsFingerprint !== fingerprint) {
        lastFacetsFingerprint = fingerprint;
        (typeof requestIdleCallback === 'function' ? requestIdleCallback : setTimeout)(() => loadExplorerFacets().catch(() => {}));
      }
    }
    function renderArtwork(game) {
      const items = artworkKinds.filter(([, , flag]) => game[flag]);
      return items.length ? `<div class="detail-card"><h3>Artwork</h3><div class="screenshot-grid">${items.map(([kind,label]) => kind === 'manual' ? `<button type="button" data-manual aria-label="Open ${escapeHtml(label)}"><div class="cover-title">${escapeHtml(label)}</div></button>` : `<button data-artwork="${kind}" aria-label="Open ${escapeHtml(label)}"><img src="${media(game,kind)}" alt="${escapeHtml(label)}" loading="lazy" decoding="async"></button>`).join('')}</div></div>` : '';
    }
    function arrangeGroups(visible) {
      const sort = $('sort').value;
      const groups = [];
      let current = '';
      visible.forEach((game, index) => {
        const label = sort === 'platform' ? (game.platform || '#') : sort === 'genre' ? (game.genre || '#') : (String(game.sort_title || game.name || '#').trim()[0] || '#').toUpperCase();
        if (label !== current) {
          current = label;
          groups.push({label, index});
        }
      });
      return groups;
    }
    function renderArrangeBar(visible) {
      const groups = arrangeGroups(visible);
      const bar = $('arrangeBar');
      if (!visible.length || groups.length < 4) {
        bar.hidden = true;
        bar.innerHTML = '';
        return;
      }
      bar.hidden = false;
      bar.innerHTML = groups.map(group => `<div class="arrange-marker" style="top:${(group.index / Math.max(visible.length - 1, 1)) * 100}%"><span>${escapeHtml(group.label)}</span></div>`).join('');
      const jumpToGroup = event => {
        if (event.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
        if (event.type === 'keydown') event.preventDefault();
        const rect = bar.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
        const target = groups.reduce((best, group) => Math.abs(group.index / Math.max(visible.length - 1, 1) - ratio) < Math.abs(best.index / Math.max(visible.length - 1, 1) - ratio) ? group : best, groups[0]);
        const pane = gridPane();
        if (pane && gridRowHeight) {
          const row = Math.floor(target.index / Math.max(gridCols, 1));
          gridScrollTop = Math.max(0, row * gridRowHeight - pane.clientHeight / 2);
          pane.scrollTop = gridScrollTop;
          renderGrid();
        } else {
          const card = document.querySelector(`[data-game="${visible[target.index].id}"]`);
          if (card) card.scrollIntoView({block:'nearest'});
        }
      };
      bar.onclick = jumpToGroup;
      bar.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') jumpToGroup({type:'keydown', key:event.key, preventDefault:() => {}, clientY:bar.getBoundingClientRect().top});
      };
    }
    function renderPlatformCategories() {
      const counts = new Map();
      AppState.games.forEach(game => {
        const name = platformCategoryFor(game);
        counts.set(name, (counts.get(name) || 0) + 1);
      });
      const items = [['all',`All (${AppState.games.length})`], ...[...counts].sort((a,b) => a[0].localeCompare(b[0])).map(([name,count]) => [name,`${name} (${count})`])];
      if (!$('platformCategories')) return;
      $('platformCategories').innerHTML = items.map(([value,label]) => `<button class="platform ${AppState.platformCategory === value ? 'active' : ''}" data-platform-category="${escapeHtml(value)}">${escapeHtml(label)}</button>`).join('');
      document.querySelectorAll('[data-platform-category]').forEach(button => button.onclick = () => { leaveActivePreset(); AppState.activePlaylist = ''; AppState.platformCategory = button.dataset.platformCategory; AppState.selectedId = null; render(); loadTheme(); });
    }
    function renderPlatforms() {
      const counts = new Map();
      AppState.games.forEach(game => counts.set(game.platform || 'Unspecified', (counts.get(game.platform || 'Unspecified') || 0) + 1));
      const items = [['all',`All (${AppState.games.length})`], ...[...counts].sort((a,b) => a[0].localeCompare(b[0])).map(([name,count]) => [name,`${name} (${count})`])];
      $('platforms').innerHTML = items.map(([value,label]) => `<button class="platform ${AppState.platform === value ? 'active' : ''}" data-platform="${escapeHtml(value)}">${escapeHtml(label)}</button>`).join('');
      $('platforms').querySelectorAll('[data-platform]').forEach(button => button.onclick = () => { leaveActivePreset(); AppState.activePlaylist = ''; AppState.platform = button.dataset.platform; AppState.selectedId = null; render(); loadTheme(); });
    }
    function renderPlaylists() {
      $('playlists').innerHTML = AppState.playlists.length ? AppState.playlists.map(item => `<div class="playlist-row"><button class="platform ${AppState.activePlaylist === item.name ? 'active' : ''}" data-playlist="${escapeHtml(item.name)}">${escapeHtml(item.name)} <small>${item.type === 'manual' ? `(${(item.members || []).length})` : '↻'}</small></button><button class="playlist-delete" data-delete-playlist="${escapeHtml(item.name)}" aria-label="Delete ${escapeHtml(item.name)}">×</button></div>`).join('') : '<span class="description">Create a playlist to pin one here.</span>';
      document.querySelectorAll('[data-playlist]').forEach(button => button.onclick = () => {
        const item = AppState.playlists.find(playlist => playlist.name === button.dataset.playlist);
        if (!item) return;
        leaveActivePreset();
        AppState.activePlaylist = item.name;
        AppState.platform = item.rules.platform || 'all';
        $('view').value = [...$('view').options].some(option => option.value === item.rules.view) ? item.rules.view : 'all';
        $('sidebarSearch').value = item.type === 'manual' ? '' : item.rules.query || '';
        render();
        loadTheme();
      });
      document.querySelectorAll('[data-delete-playlist]').forEach(button => button.onclick = () => deletePlaylist(button.dataset.deletePlaylist));
    }
    function renderFilterPresets() {
      const container = $('filterPresets');
      if (!container) return;
      container.innerHTML = AppState.filterPresets.length ? AppState.filterPresets.map(item => `<div class="playlist-row"><button class="platform ${AppState.activeFilterPreset === item.name ? 'active' : ''}" data-filter-preset="${escapeHtml(item.name)}">${escapeHtml(item.name)}${item.bigbox_quick ? ' ★' : ''}</button><button class="playlist-delete" data-delete-preset="${escapeHtml(item.name)}" aria-label="Delete ${escapeHtml(item.name)}">×</button></div>`).join('') : '<span class="description">Save a preset to pin filters.</span>';
      document.querySelectorAll('[data-filter-preset]').forEach(button => button.onclick = () => {
        const item = AppState.filterPresets.find(preset => preset.name === button.dataset.filterPreset);
        if (!item) return;
        AppState.activeFilterPreset = item.name;
        AppState.activePlaylist = '';
        AppState.explorerRules = {};
        const rules = item.rules || {};
        AppState.platform = rules.platform || 'all';
        AppState.platformCategory = rules.platform_category || 'all';
        if (rules.view) $('view').value = [...$('view').options].some(option => option.value === rules.view) ? rules.view : 'all';
        if (rules.query !== undefined) $('sidebarSearch').value = rules.query || '';
        if (rules.esrb !== undefined && $('esrbFilter')) $('esrbFilter').value = rules.esrb || '';
        render();
        loadTheme();
      });
      document.querySelectorAll('[data-delete-preset]').forEach(button => button.onclick = async () => {
        const ok = await confirmAction({
          title: 'Delete preset',
          target: button.dataset.deletePreset,
          consequence: 'This filter preset will be removed from the library.',
          retained: 'Saved games and other presets remain.',
          recovery: 'Save a new preset from the current filters.',
          destructive: true,
          confirmLabel: 'Delete',
        });
        if (!ok) return;
        try {
          await api('/api/filter-presets/delete',{method:'POST',body:JSON.stringify({name:button.dataset.deletePreset})});
          if (AppState.activeFilterPreset === button.dataset.deletePreset) AppState.activeFilterPreset = '';
          await refresh();
          notify('Preset deleted');
        } catch(error) { notify(error.message); }
      });
      const quick = $('bigBoxQuickPreset');
      if (quick) {
        const quickItems = AppState.filterPresets.filter(item => item.bigbox_quick);
        quick.innerHTML = '<option value="">None</option>' + quickItems.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('');
      }
    }
    function selectGame(id) {
      const pane = gridPane();
      if (pane) detailSheetScrollTop = pane.scrollTop;
      AppState.selectedId = id;
      document.querySelectorAll('[data-game]').forEach(item => {
        const row = item.closest('.card,.list-row') || item;
        row.classList.toggle('selected', Number(item.dataset.game) === id);
      });
      renderDetails();
    }
    function imageMarkup(game, imageGroup) {
      const flags = {cover:'has_cover',background:'has_background',screenshot:'available_screenshots',clear_logo:'has_clear_logo',fanart:'has_fanart',banner:'has_banner',icon:'has_icon',box_back:'has_box_back',box_spine:'has_box_spine',box_3d:'has_box_3d',title_screen:'has_title_screen',cart_front:'has_cart_front',cart_back:'has_cart_back',disc:'has_disc',advertisement:'has_advertisement',manual:'has_manual'};
      const flag = flags[imageGroup];
      const available = imageGroup === 'screenshot' ? game.available_screenshots?.length : game[flag];
      if (imageGroup === 'manual') {
        if (available) return `<div class="cover manual-tile" data-manual-tile="${game.id}" role="button" tabindex="0" aria-label="Open manual for ${escapeHtml(game.name)}"><div class="cover-title">Manual</div></div>`;
        return `<div class="cover-title">${escapeHtml(game.name)}</div>`;
      }
      if (available) {
        const index = imageGroup === 'screenshot' ? game.available_screenshots[0] : '';
        return `<img src="${media(game,imageGroup,index)}" alt="" loading="lazy" decoding="async" data-gid="${game.id}">`;
      }
      return `<div class="cover-title">${escapeHtml(game.name)}</div>`;
    }
    // Cover shape tracking: the load listener + render sweep record each cover's
    // natural aspect ratio (w/h) per game id. Unknown covers default to the
    // .cover-title fallback shape (portrait, aspect-ratio .72).
    let ratioRegroupTimer = null;
    let _coverRatiosRevision = 0;
    // Cover images load async; when a newly measured ratio moves a game into a
    // different bucket than the one the grid was rendered with, regroup once.
    // Debounced so lazy loads (which fire as covers scroll into view) batch
    // into a single render instead of one re-render per image.
    const recordCoverRatio = img => {
      if (img.naturalWidth <= 32) return;
      const gid = img.dataset.gid;
      const ratio = img.naturalWidth / img.naturalHeight;
      const prev = AppState.coverRatios[gid];
      AppState.coverRatios[gid] = ratio;
      _coverRatiosRevision++;
      if (prev !== ratio && coverBucketOf(prev) !== coverBucketOf(ratio)) {
        clearTimeout(ratioRegroupTimer);
        ratioRegroupTimer = setTimeout(() => { ratioRegroupTimer = null; renderGrid(); }, 150);
      }
    };
    function groupedSections(visible) {
      const buckets = {portrait:[],square:[],landscape:[]};
      visible.forEach(game => buckets[coverBucketOf(AppState.coverRatios[game.id])].push(game));
      const sections = [];
      for (const [key,label] of RATIO_BUCKETS) if (buckets[key].length) sections.push({key,label,games:buckets[key]});
      return sections;
    }
    function gridCellWidth() {
      const grid = $('grid');
      const style = getComputedStyle(grid);
      const gap = parseFloat(style.columnGap) || 0;
      const cols = Math.max(1, Math.floor((grid.clientWidth + gap) / (132 + gap)));
      return [(grid.clientWidth - (cols - 1) * gap) / cols, cols];
    }
    let groupedGeo = null, textBlockH = 62, ratioHeadH = 30;
    let _gridGeoCache = null;
    function gridRowsGeometry(sections) {
      const [cellW, cols] = gridCellWidth();
      const grouping = AppState.appSettings.cover_grouping || 'shape';
      // Lightweight fingerprint: section key + game count + first/last id.
      const sectionFp = sections.map(s => `${s.key}:${s.games.length}:${s.games.length ? s.games[0].id : ''}:${s.games.length ? s.games[s.games.length - 1].id : ''}`).join('|');
      const key = `${grouping}\0${cols}\0${cellW.toFixed(1)}\0${_coverRatiosRevision}\0${sectionFp}`;
      if (_gridGeoCache && _gridGeoCache.key === key) return _gridGeoCache.result;
      const rowGap = parseFloat(getComputedStyle($('grid')).rowGap) || 0;
      const rows = [];
      let top = 0;
      for (const section of sections) {
        rows.push({kind:'header', label:section.label, count:section.games.length, top, height:ratioHeadH});
        top += ratioHeadH + rowGap;
        const cardRows = Math.ceil(section.games.length / cols);
        for (let r = 0; r < cardRows; r++) {
          const games = section.games.slice(r * cols, (r + 1) * cols);
          const minRatio = Math.min(...games.map(game => AppState.coverRatios[game.id] || RATIO_REP[section.key]));
          rows.push({kind:'cards', section, games, top, height:cellW / minRatio + textBlockH});
          top += cellW / minRatio + textBlockH + rowGap;
        }
      }
      const result = {rows, cols, totalHeight: top};
      _gridGeoCache = { key, result };
      return result;
    }
    function renderGroupedGrid(visible, imageGroup, fromScroll, motionClass) {
      const sections = groupedSections(visible);
      const {rows, cols, totalHeight} = gridRowsGeometry(sections);
      if (!isVirtualEnabled()) {
        let cardIndex = 0;
        const rendered = rows.map(row => row.kind === 'header'
          ? `<div class="ratio-head">${row.label}<span class="ratio-count">${row.count}</span></div>`
          : row.games.map(game => gridCardHTML(game, cardIndex++, imageGroup, fromScroll, motionClass, row.section.key)).join('')).join('');
        return {topSpacer:'', bottomSpacer:'', rendered, geometry:{rows, cols, totalHeight}};
      }
      const pane = gridPane();
      const paneHeight = pane ? pane.clientHeight : 0;
      const maxRow = Math.max(200, ...rows.map(row => row.height));
      const topLimit = gridScrollTop - 2 * maxRow, bottomLimit = gridScrollTop + paneHeight + 2 * maxRow;
      const windowRows = rows.filter(row => row.top + row.height > topLimit && row.top < bottomLimit);
      const firstTop = windowRows.length ? windowRows[0].top : 0;
      const lastBottom = windowRows.length ? windowRows[windowRows.length - 1].top + windowRows[windowRows.length - 1].height : 0;
      let cardIndex = 0;
      const rendered = windowRows.map(row => row.kind === 'header'
        ? `<div class="ratio-head">${row.label}<span class="ratio-count">${row.count}</span></div>`
        : row.games.map(game => gridCardHTML(game, cardIndex++, imageGroup, fromScroll, motionClass, row.section.key)).join('')).join('');
      return {topSpacer:`<div class="grid-spacer" style="height:${firstTop}px;contain-intrinsic-size:auto ${firstTop}px"></div>`, bottomSpacer:`<div class="grid-spacer" style="height:${Math.max(0, totalHeight - lastBottom)}px;contain-intrinsic-size:auto ${Math.max(0, totalHeight - lastBottom)}px"></div>`, rendered, geometry:{rows, cols, totalHeight}};
    }
    function gridCardHTML(game, index, imageGroup, fromScroll, motionClass, bucketKey = '') {
      return `<article class="card${motionClass} ${AppState.selectedId === game.id || selectedIds.has(game.id) ? 'selected' : ''}"${bucketKey ? ` data-ratio="${bucketKey}"` : ''}${fromScroll ? '' : ` style="--motion-index:${Math.min(index,10)}"`}>
        ${AppState.bulkMode ? `<input class="card-picker" type="checkbox" data-game-picker="${game.id}" ${selectedIds.has(game.id) ? 'checked' : ''} aria-label="Select ${escapeHtml(game.name)}">` : ''}
        <button type="button" class="card-main" data-game="${game.id}" aria-label="Open ${escapeHtml(game.name)}"><div class="cover ${AppState.appSettings.bigbox_mode === 'coverflow' ? 'jewel-3d' : ''}">${imageMarkup(game,imageGroup)}</div>
        <h3>${escapeHtml(game.name)}</h3><p>${escapeHtml(game.developer || game.platform || '')}</p>
        <div class="badge-row">${renderBadges(game)}</div></button>
      </article>`;
    }
    let gridScrollTop = 0, gridRowHeight = 0, gridCols = 1;
    const gridPane = () => document.querySelector('main.library');
    function measureGridLayout() {
      const grid = $('grid');
      if (!grid) return;
      const cards = grid.querySelectorAll('.card, .list-row');
      const firstCard = cards[0];
      if (!firstCard) { gridRowHeight = 0; gridCols = 1; return; }
      const style = getComputedStyle(grid);
      const rowGap = parseFloat(style.rowGap) || 0;
      if (grid.classList.contains('list-view')) { gridCols = 1; gridRowHeight = firstCard.offsetHeight + rowGap; return; }
      const columnGap = parseFloat(style.columnGap) || 0;
      gridCols = Math.max(1, Math.floor((grid.clientWidth + columnGap) / (firstCard.offsetWidth + columnGap)));
      if (groupedGeo) {
        gridCols = groupedGeo.cols;
        const card = grid.querySelector('.card');
        if (card) {
          const cover = card.querySelector('.cover');
          if (cover) textBlockH = card.offsetHeight - cover.offsetHeight;
        }
        const head = grid.querySelector('.ratio-head');
        if (head) ratioHeadH = head.offsetHeight;
        gridRowHeight = Math.max(200, ...groupedGeo.rows.map(row => row.height));
        return;
      }
      // Grid rows are sized by their tallest card. Natural-ratio covers make
      // card heights vary, so use the tallest card in the first row instead of
      // a single-card sample, otherwise the virtual window drifts on scroll.
      let rowHeight = 0;
      for (let i = 0; i < Math.min(gridCols, cards.length); i++) rowHeight = Math.max(rowHeight, cards[i].offsetHeight);
      gridRowHeight = rowHeight + rowGap;
    }
    function gridWindow(total) {
      if (!isVirtualEnabled()) return [0, total];
      if (!gridRowHeight) return [0, total];
      const pane = gridPane();
      const paneHeight = pane ? pane.clientHeight : 0;
      const rows = Math.ceil(total / Math.max(gridCols, 1));
      const firstRow = Math.max(0, Math.floor(gridScrollTop / gridRowHeight) - 2);
      const lastRow = Math.min(rows - 1, Math.ceil((gridScrollTop + paneHeight) / gridRowHeight) + 2);
      return [Math.min(total, firstRow * gridCols), Math.min(total, (lastRow + 1) * gridCols)];
    }
    function renderGrid({fromScroll} = {}) {
      const pane = gridPane();
      if (pane) gridScrollTop = pane.scrollTop;
      const visible = visibleGames();
      const explicitImageGroup = AppState.activePlaylist ? AppState.appSettings.image_group_by_playlist?.[AppState.activePlaylist] : AppState.platform !== 'all' ? AppState.appSettings.image_group_by_platform?.[AppState.platform] : AppState.appSettings.image_group;
      const effectiveImageGroup = explicitImageGroup || (AppState.platform === 'all' && !AppState.activePlaylist ? 'cover' : 'default');
      const imageGroup = effectiveImageGroup === 'default' ? AppState.appSettings.image_group || 'cover' : effectiveImageGroup;
      // Scroll-triggered renders only repaint the virtual window. The chrome
      // below is state-driven and never changes while scrolling, so touching it
      // on every row crossing is pure layout waste on the slow webview.
      if (!fromScroll) {
        $('imageGroup').value = effectiveImageGroup;
        $('grouping').value = AppState.appSettings.cover_grouping || 'shape';
        $('bulkButton').textContent = AppState.bulkMode ? selectedIds.size ? `Edit ${selectedIds.size} Selected` : 'Cancel Bulk Edit' : 'Bulk Edit';
        $('libraryTitle').textContent = AppState.activeFilterPreset || AppState.activePlaylist || (AppState.platform === 'all' ? $('view').selectedOptions[0].text : AppState.platform);
        $('libraryMeta').textContent = AppState.bulkMode ? `${selectedIds.size} selected · ${visible.length} shown` : `${visible.length} game${visible.length === 1 ? '' : 's'}`;
        $('surpriseButton').disabled = !visible.length;
        $('status').textContent = `${AppState.games.length} games · local library`;
      }
      if (!visible.length) {
        $('grid').className = 'grid';
        $('grid').innerHTML = AppState.games.length
          ? `<div class="empty"><div><h2>No games match this view</h2><p>Change the active filters or search the library again.</p></div></div>`
          : `<div class="empty"><div><h2>Start your library</h2><p>Bring your games into OpenBox, then search, filter, and launch them from one collection.</p><div class="empty-actions"><button id="emptySetupLibrary">Set up library</button><button id="emptyAdd">Add game</button><button class="empty-secondary" id="emptyImport">Import folder</button><button class="empty-secondary" id="emptySteam">Import Steam</button><button class="empty-secondary" id="emptyHeroic">Import Heroic</button><button class="empty-secondary" id="emptyLutris">Import Lutris</button></div></div></div>`;
        if ($('emptySetupLibrary')) $('emptySetupLibrary').onclick = () => $('setupCenter').showModal();
        if ($('emptyAdd')) $('emptyAdd').onclick = () => openGameDialog();
        if ($('emptyImport')) $('emptyImport').onclick = () => importFolder();
        if ($('emptySteam')) $('emptySteam').onclick = () => importSteam();
        if ($('emptyHeroic')) $('emptyHeroic').onclick = () => importHeroic();
        if ($('emptyLutris')) $('emptyLutris').onclick = () => importLutris();
        gridRowHeight = 0;
        renderArrangeBar(visible);
        return;
      }
      const listView = (AppState.appSettings.library_view || 'grid') === 'list';
      $('grid').className = listView ? 'list-view' : 'grid';
      // contain-intrinsic-size bookkeeping for virtual rows (skip when virtual disabled)
      if (isVirtualEnabled()) $('grid').style.containIntrinsicSize = 'auto 800px';
      else $('grid').style.containIntrinsicSize = '';
      const total = visible.length;
      const grouped = !listView && (AppState.appSettings.cover_grouping || 'shape') === 'shape';
      // Entrance animation only runs on full (state-driven) renders; scroll
      // renders must not re-trigger the staggered surface-in on every row
      // crossing. fromScroll drops both the class and the inline delay.
      const motionClass = fromScroll ? '' : ' motion-enter';
      let topSpacer = '', bottomSpacer = '', rendered = '';
      if (grouped) {
        const result = renderGroupedGrid(visible, imageGroup, fromScroll, motionClass);
        topSpacer = result.topSpacer; bottomSpacer = result.bottomSpacer; rendered = result.rendered;
        groupedGeo = result.geometry;
      } else {
        groupedGeo = null;
        const [start, end] = gridWindow(total);
        const rows = Math.ceil(total / Math.max(gridCols, 1));
        const topHeight = Math.floor(start / Math.max(gridCols, 1)) * gridRowHeight;
        const bottomHeight = gridRowHeight ? Math.max(0, rows - Math.ceil(end / Math.max(gridCols, 1))) * gridRowHeight : 0;
        if (!isVirtualEnabled()) {
          topSpacer = ''; bottomSpacer = '';
        } else {
          topSpacer = gridRowHeight ? `<div class="grid-spacer" style="height:${topHeight}px;contain-intrinsic-size:auto ${topHeight}px"></div>` : '';
          bottomSpacer = gridRowHeight ? `<div class="grid-spacer" style="height:${bottomHeight}px;contain-intrinsic-size:auto ${bottomHeight}px"></div>` : '';
        }
        const chunk = isVirtualEnabled() ? visible.slice(start, end) : visible;
        rendered = chunk.map((game,index) => listView
           ? `<button type="button" class="list-row${motionClass} ${AppState.selectedId === game.id || selectedIds.has(game.id) ? 'selected' : ''}"${fromScroll ? '' : ` style="--motion-index:${Math.min(index,10)}"`} data-game="${game.id}" aria-label="Open ${escapeHtml(game.name)}"><strong>${escapeHtml(game.name)}<span class="badge-row">${renderBadges(game)}</span></strong><span>${escapeHtml(game.platform || '')}</span><span>${escapeHtml(game.genre || '')}</span><span>${escapeHtml(game.esrb || '-')}</span><span>${escapeHtml(game.progress || '')}</span><span>${game.play_count || 0}</span><span>${game.rating || ''}</span></button>`
           : gridCardHTML(game, index, imageGroup, fromScroll, motionClass)).join('');
      }
      const focusedGameId = $('grid')?.contains(document.activeElement) ? document.activeElement?.closest?.('[data-game]')?.dataset.game : null;
      const focusedPickerId = $('grid')?.contains(document.activeElement) ? document.activeElement?.closest?.('[data-game-picker]')?.dataset.gamePicker : null;
      $('grid').innerHTML = listView ? `<div class="list-head"><span>Title</span><span>Platform</span><span>Genre</span><span>ESRB</span><span>Progress</span><span>Plays</span><span>Rating</span></div>${topSpacer}${rendered}${bottomSpacer}` : `${topSpacer}${rendered}${bottomSpacer}`;
      // After virtual render, observe spacers via IntersectionObserver
      if (isVirtualEnabled()) ensureVirtualObserver();
      document.querySelectorAll('#grid img[data-gid]').forEach(img => { if (img.complete) recordCoverRatio(img); });
      // Set fetchPriority via DOM property to avoid per-render string churn.
      if (typeof HTMLImageElement !== 'undefined' && 'fetchPriority' in HTMLImageElement.prototype) {
        document.querySelectorAll('#grid img[data-gid]').forEach(img => { img.fetchPriority = Number(img.dataset.gid) === AppState.selectedId ? 'high' : 'low'; });
      }
      document.querySelectorAll('[data-game]').forEach(card => card.onclick = event => {
        const id = Number(card.dataset.game);
        if (event.shiftKey || event.ctrlKey || event.metaKey || AppState.bulkMode) {
          if (event.shiftKey && AppState.selectedId !== null) {
            const ids = visible.map(game => game.id);
            const startIndex = ids.indexOf(AppState.selectedId), endIndex = ids.indexOf(id);
            if (startIndex >= 0 && endIndex >= 0) ids.slice(Math.min(startIndex,endIndex), Math.max(startIndex,endIndex) + 1).forEach(value => selectedIds.add(value));
          } else {
            selectedIds.has(id) ? selectedIds.delete(id) : selectedIds.add(id);
          }
          AppState.selectedId = id;
          if (!AppState.bulkMode) { AppState.bulkMode = true; notify('Selection mode enabled. Use Bulk Edit to apply changes.'); }
          renderGrid();
        } else {
          selectGame(id);
        }
      });
      document.querySelectorAll('[data-game-picker]').forEach(input => input.onchange = () => {
        const id = Number(input.dataset.gamePicker);
        input.checked ? selectedIds.add(id) : selectedIds.delete(id);
        input.closest('.card')?.classList.toggle('selected', input.checked || AppState.selectedId === id);
      });
      document.querySelectorAll('[data-manual-tile]').forEach(tile => {
        const game = AppState.games.find(item => item.id === Number(tile.dataset.manualTile));
        if (!game) return;
        const open = event => { event.stopPropagation(); openManualReader(game); };
        tile.onclick = open;
        tile.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(event); } };
      });
      renderArrangeBar(visible);
      markFilterAria();
      if (focusedGameId && (!document.activeElement || document.activeElement === document.body)) {
        document.querySelector(`[data-game="${focusedGameId}"]`)?.focus({ preventScroll: true });
      } else if (focusedPickerId && (!document.activeElement || document.activeElement === document.body)) {
        document.querySelector(`[data-game-picker="${focusedPickerId}"]`)?.focus({ preventScroll: true });
      }
      const measuredBefore = gridRowHeight;
      measureGridLayout();
      if (!measuredBefore && gridRowHeight && total) renderGrid({fromScroll});
    }
    function renderDetails() {
      const game = AppState.games.find(item => item.id === AppState.selectedId);
      if (!game && (AppState.platform !== 'all' || AppState.platformCategory !== 'all' || AppState.activePlaylist)) {
        if (AppState.activePlaylist) renderCollectionDetails(AppState.activePlaylist, visibleGames(), 'Playlist');
        else if (AppState.platformCategory !== 'all') renderCollectionDetails(AppState.platformCategory, AppState.games.filter(item => platformCategoryFor(item) === AppState.platformCategory), 'Category');
        else renderPlatformDetails(AppState.platform);
        return;
      }
      if (!game) {
        const pane = gridPane();
        if (pane && detailSheetScrollTop) pane.scrollTop = detailSheetScrollTop;
        $('details').innerHTML = '<div class="detail-empty">Select a game to inspect its real metadata and artwork.</div>';
        applyDetailsLayout();
        return;
      }
      const applications = game.applications || [];
      const versions = game.versions || [];
      const documents = game.documents || [];
      const screenshots = game.available_screenshots || [];
      const savePaths = game.save_paths || [];
      const heroStyle = game.has_background ? `style="background-image:url('${media(game,'background')}')"` : '';
      $('details').innerHTML = `<div class="hero motion-enter" ${heroStyle}><div class="hero-copy"><div class="hero-kicker">${escapeHtml(game.platform || 'Unspecified platform')}</div><h2>${escapeHtml(game.name)}</h2></div></div>
        <div class="detail-body">
          <div class="rating"><strong>${game.favorite ? '★ Favorite' : game.rating ? `${game.rating} ★` : 'Library'}</strong><span>${escapeHtml(game.progress || game.genre || '')}</span><span class="badge-row">${renderBadges(game)}</span></div>
          <button class="play" id="playButton" ${game.path_exists && game.store_installed !== false ? '' : game.gameyfin_id && !game.store_installed ? '' : 'disabled'}>${game.gameyfin_id && !game.store_installed ? '⬇ INSTALL' : '▶ PLAY'}</button>
          <div class="detail-actions"><button class="icon-button" id="favoriteButton">${game.favorite ? 'Remove favorite' : 'Add favorite'}</button><button class="icon-button" id="editButton">Edit metadata</button><button class="icon-button" id="databaseMetadataButton">Find metadata</button>${game.steam_app_id ? '<button class="icon-button" id="steamMetadataButton">Use Steam data</button>' : ''}<button class="icon-button" id="captureScreenshot">Capture screenshot</button><button class="icon-button" id="downloadBezel">Download bezel</button>${game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="uninstallGameyfin">Uninstall Gameyfin copy</button>' : ''}${game.path ? '<button class="icon-button" id="showInFolderButton">Show in folder</button>' : ''}<button class="icon-button" id="removeGameButton">Remove game</button></div>
          <div class="detail-card"><h3>Information</h3><div class="facts">
            ${fact('Release date',game.year)}${fact('Developer',game.developer)}${fact('Publisher',game.publisher)}${fact('ESRB',game.esrb)}${fact('Source',game.source)}${fact('Category',platformCategoryFor(game))}${Object.entries(game.custom_fields || {}).map(([key,value]) => fact(key,value)).join('')}${fact('Max players',game.max_players)}${fact('Controller support',game.controller_support)}${fact('Disc count',game.disc_count)}${fact('Play time',duration(game.playtime_seconds))}
            ${fact('Launches',game.play_count)}${fact('Last played',game.last_played ? game.last_played.replace('T',' ') : '')}${fact('Progress',game.progress)}${fact('Rating',game.rating ? `${game.rating} / 5` : '')}${fact('Region',game.region)}${fact('Play mode',game.play_mode)}${fact('Wikipedia',game.wikipedia_url)}${fact('Video URL',game.video_url)}
          </div>${game.description ? `<p class="description">${escapeHtml(game.description)}</p>` : ''}${game.notes ? `<p class="description"><strong>Notes</strong><br>${escapeHtml(game.notes)}</p>` : ''}
          ${applications.length || versions.length || documents.length ? `<div class="extras">${applications.map((item,index) => `<button class="icon-button" data-extra="applications:${index}">App · ${escapeHtml(item.name)}</button>`).join('')}${versions.map((item,index) => `<button class="icon-button" data-extra="versions:${index}">Version · ${escapeHtml(item.name)}</button>`).join('')}${documents.map((item,index) => `<button class="icon-button" data-document="${index}">Read ${escapeHtml(item.name)}</button><button class="icon-button" data-extra="documents:${index}" aria-label="Open ${escapeHtml(item.name)} externally">↗</button>`).join('')}</div>` : ''}</div>
          ${game.has_video ? `<div class="detail-card"><h3>Video</h3><video class="media-player" controls preload="metadata" src="${media(game,'video')}"></video></div>` : ''}
          ${game.has_music ? `<div class="detail-card"><h3>Music</h3><audio class="media-player" controls preload="metadata" src="${media(game,'music')}"></audio></div>` : ''}
          ${screenshots.length ? `<div class="detail-card"><h3>Screenshots</h3><div class="screenshot-grid">${screenshots.map(index => `<button data-screenshot="${index}" aria-label="Open screenshot ${index + 1}"><img src="${media(game,'screenshot',index)}" alt="" loading="lazy" decoding="async"></button>`).join('')}</div></div>` : ''}
          ${renderArtwork(game)}
          <div class="detail-card"><h3>Related Games</h3><div class="related-grid" id="relatedGames"><span class="description">Finding matches...</span></div></div>
          ${AppState.raConfigured ? `<div class="detail-card"><h3>RetroAchievements</h3><div id="achievementContent"><p class="description">${game.ra_game_id ? `Matched to game ${escapeHtml(game.ra_game_id)}.` : 'Match this ROM to load achievements.'}</p><div class="extras">${game.steam_app_id ? '<button class="icon-button" id="downloadTrailer">Download Steam trailer</button>' : ''}${game.heroic_app_id ? '<button class="icon-button" id="downloadGogMedia">Download GOG media</button>' : ''}<button class="icon-button" id="openBrowser">Open Wikipedia</button></div><button class="icon-button" id="loadAchievements">Load achievements</button><div id="achievementStats"></div></div></div>` : ''}
          <div class="detail-card"><h3>Save management</h3><div class="extras"><button class="icon-button" id="discoverSaves">Discover locations</button>${savePaths.length ? '<button class="icon-button" id="backupSaves">Back up now</button>' : ''}<button class="icon-button" id="ludusaviBackup">Ludusavi backup</button><button class="icon-button" id="ludusaviRestore">Ludusavi restore</button>${AppState.appSettings.save_tools?.hoard ? '<button class="icon-button" id="hoardBackup">Hoard backup</button>' : ''}${game.platform === 'Arcade' || game.rom_name ? '<button class="icon-button" id="exportHighscores">Export high scores</button>' : ''}</div><div class="description" id="saveDiscovery">${savePaths.length ? `${savePaths.length} configured location${savePaths.length === 1 ? '' : 's'}` : 'No save location configured.'}${AppState.appSettings.save_tools?.ludusavi ? ' · Ludusavi detected' : ' · Install ludusavi for automatic save discovery'}</div><div class="extras" id="saveBackups"></div></div>
          <div class="detail-card doctor-card" id="doctorCard" style="border:1px solid var(--border-card)"><h3>Launch Doctor</h3><div id="doctorChecks" class="description">Checking launch readiness…</div></div>
        </div>`;
      $('playButton').onclick = () => {
        if (game.gameyfin_id && !game.store_installed) installGameyfin(game);
        else launch(game.id, $('playButton'));
      };
      $('favoriteButton').onclick = () => favorite(game.id);
      $('editButton').onclick = () => openGameDialog(game);
      $('databaseMetadataButton').onclick = () => openMetadata(game);
      if ($('steamMetadataButton')) $('steamMetadataButton').onclick = () => steamMetadata(game.id);
      if ($('captureScreenshot')) $('captureScreenshot').onclick = () => captureScreenshot(game.id);
      if ($('downloadBezel')) $('downloadBezel').onclick = () => downloadBezel(game.platform);
      if ($('uninstallGameyfin')) $('uninstallGameyfin').onclick = () => uninstallGameyfin(game);
      if ($('exportHighscores')) $('exportHighscores').onclick = async () => { try { const result = await api('/api/highscores/export',{method:'POST',body:JSON.stringify({id:game.id})}); notify(`Exported ${result.files.length} high score file${result.files.length === 1 ? '' : 's'}`); } catch(error) { notify(error.message); } };
      if ($('showInFolderButton')) $('showInFolderButton').onclick = () => nativeReveal(game.path);
      $('removeGameButton').onclick = () => removeGame(game.id,game.name);
      document.querySelectorAll('[data-extra]').forEach(button => button.onclick = () => {
        const [kind,index] = button.dataset.extra.split(':');
        launchExtra(game.id,kind,Number(index));
      });
      document.querySelectorAll('[data-screenshot]').forEach(button => button.onclick = () => {
        const image = document.createElement('img');
        image.id = 'fullScreenshot';
        image.alt = 'Game screenshot';
        image.decoding = 'async';
        image.src = media(game,'screenshot',button.dataset.screenshot);
        $('mediaDialog').querySelectorAll('img').forEach(el => el.remove());
        $('mediaDialog').append(image);
        $('mediaDialog').showModal();
      });
      document.querySelectorAll('[data-artwork]').forEach(button => button.onclick = () => {
        const image = document.createElement('img');
        image.id = 'fullScreenshot';
        image.alt = button.getAttribute('aria-label') || 'Game artwork';
        image.decoding = 'async';
        image.src = media(game,button.dataset.artwork);
        $('mediaDialog').querySelectorAll('img').forEach(el => el.remove());
        $('mediaDialog').append(image);
        $('mediaDialog').showModal();
      });
      document.querySelectorAll('[data-manual]').forEach(button => button.onclick = () => openManualReader(game));
      document.querySelectorAll('[data-manual-tile]').forEach(button => button.onclick = () => openManualReader(game));
      document.querySelectorAll('[data-document]').forEach(button => button.onclick = () => openReader(game, Number(button.dataset.document)));
      if ($('loadAchievements')) $('loadAchievements').onclick = () => loadAchievements(game.id);
      if ($('downloadTrailer')) $('downloadTrailer').onclick = async () => { try { await api('/api/metadata/trailer',{method:'POST',body:JSON.stringify({id:game.id})}); await refresh(); renderDetails(); notify('Steam trailer downloaded'); } catch(error) { notify(error.message); } };
      if ($('downloadGogMedia')) $('downloadGogMedia').onclick = async () => { try { await api('/api/metadata/gog',{method:'POST',body:JSON.stringify({id:game.id})}); await refresh(); renderDetails(); notify('GOG media downloaded'); } catch(error) { notify(error.message); } };
      if ($('openBrowser')) $('openBrowser').onclick = () => { const url = game.wikipedia_url || `https://en.wikipedia.org/wiki/Special:Search?search=${encodeURIComponent(game.name)}`; nativeOpenExternal(url); };
      if ($('backupSaves')) {
        $('backupSaves').onclick = () => backupSaves(game.id);
        loadBackups(game.id);
      }
      if ($('ludusaviBackup')) $('ludusaviBackup').onclick = () => ludusaviAction(game.id, 'backup');
      if ($('ludusaviRestore')) $('ludusaviRestore').onclick = () => ludusaviAction(game.id, 'restore');
      if ($('hoardBackup')) $('hoardBackup').onclick = () => hoardAction(game.id, 'backup');
      $('discoverSaves').onclick = () => discoverSaves(game.id);
      loadRelated(game.id);
      loadDoctor(game);
    }
    async function loadDoctor(game) {
      const container = document.getElementById('doctorChecks');
      if (!container || !game?.game_id) { if (container) container.textContent = 'No game selected.'; return; }
      try {
        const pre = await api('/api/v2/launch/preflight', {method:'POST', body: JSON.stringify({game_id: game.game_id})});
        renderDoctorChecks(container, pre, game);
      } catch (error) {
        container.innerHTML = `<span class="description">Doctor unavailable: ${escapeHtml(error.message)}</span>`;
      }
    }
    function renderDoctorChecks(container, preflight, game) {
      const checks = preflight.checks || [];
      if (!checks.length) {
        container.innerHTML = '<span class="description" style="color:var(--brand)">Ready to launch ✓</span>';
        return;
      }
      container.innerHTML = checks.map(check => {
        const fix = check.fix_action || {};
        const kind = fix.kind || '';
        const payload = fix.payload || {};
        let fixBtn = '';
        if (kind === 'flatpak_install') {
          const app = payload.app_id || payload.flatpak_app_id || '';
          fixBtn = `<button type="button" class="primary" data-fix="flatpak" data-app="${escapeHtml(app)}" style="background:var(--brand);border:1px solid var(--focus);color:var(--white)">Install ${escapeHtml(app || 'emulator')}</button>`;
        } else if (kind === 'reveal_bios_path') {
          const p = payload.path || payload.name || '';
          fixBtn = `<button type="button" class="icon-button" data-fix="reveal" data-path="${escapeHtml(p)}" style="border:1px solid var(--focus);color:var(--focus)">Show BIOS folder</button>`;
        } else if (kind === 'pick_core') {
          const platforms = payload.platforms || payload.candidates || [];
          if (check.code === 'AMBIGUOUS_PLATFORM' && platforms.length) {
            const chips = platforms.map(pl => `<button type="button" class="platform" data-pick-platform="${escapeHtml(pl)}" data-game-id="${escapeHtml(game.game_id)}" style="border-color:var(--focus);color:var(--focus);background:var(--surface-card)">${escapeHtml(pl)}</button>`).join('');
            fixBtn = `<div class="extras" style="gap:0.4rem;flex-wrap:wrap">${chips}</div>`;
          } else if (payload.core) {
            fixBtn = `<button type="button" class="icon-button" data-fix="pick-core" data-core="${escapeHtml(payload.core)}" style="border:1px solid var(--brand);color:var(--brand)">Choose core</button>`;
          } else {
            fixBtn = `<button type="button" class="icon-button" data-fix="pick" style="border:1px solid var(--brand);color:var(--brand)">Choose emulator</button>`;
          }
        } else if (kind === 'explain_token') {
          const inv = (payload.invalid_tokens || []).join(', ');
          fixBtn = `<button type="button" class="icon-button" data-fix="explain" data-tokens="${escapeHtml(inv)}" style="border:1px solid var(--focus);color:var(--focus)">Explain token</button>`;
        } else {
          fixBtn = `<span class="description">${escapeHtml(fix.label || '')}</span>`;
        }
        const severityColor = check.severity === 'error' ? 'var(--danger)' : 'var(--gold)';
        return `<div class="doctor-row" style="border-left:3px solid ${severityColor};padding:0.5rem;margin:0.5rem 0;background:var(--surface-card)"><div><strong>${escapeHtml(check.code)}</strong>: ${escapeHtml(check.message)}</div><div style="margin-top:0.4rem">${fixBtn}</div></div>`;
      }).join('');
      // Bind fix actions
      container.querySelectorAll('[data-fix="flatpak"]').forEach(btn => btn.onclick = async () => {
        const app = btn.dataset.app;
        if (!app) return;
        try { await api('/api/emulators/install', {method:'POST', body: JSON.stringify({app_id: app})}); notify('Installing ' + app); } catch (e) { notify(e.message); }
      });
      container.querySelectorAll('[data-fix="reveal"]').forEach(btn => btn.onclick = () => {
        const p = btn.dataset.path;
        if (p) { try { nativeReveal(p); } catch (e) { notify('BIOS path: ' + p); } }
        else notify('No path to reveal');
      });
      container.querySelectorAll('[data-pick-platform]').forEach(btn => btn.onclick = async () => {
        const platform = btn.dataset.pickPlatform;
        const gid = btn.dataset.gameId;
        try {
          const g = AppState.games.find(x => String(x.game_id) === String(gid));
          if (!g) return notify('Game not found');
          await api('/api/game', {method:'POST', body: JSON.stringify({id: g.id, game: {...g, platform}})} );
          await refresh();
          notify('Platform set to ' + platform);
        } catch (e) { notify(e.message); }
      });
      container.querySelectorAll('[data-fix="pick-core"], [data-fix="pick"]').forEach(btn => btn.onclick = () => {
        // Open profiles/emulator catalog for picking
        try { document.getElementById('profilesDialog')?.showModal(); } catch (e) { notify('Open Emulator profiles to choose'); }
      });
      container.querySelectorAll('[data-fix="explain"]').forEach(btn => btn.onclick = () => {
        const toks = btn.dataset.tokens || '';
        notify('Unknown tokens: ' + toks + '. Valid: {path} {name} {platform} etc. See launch_tokens docs.');
      });
    }
    async function loadRelated(id) {
      try {
        const result = await api(`/api/related/rich?id=${id}`);
        if (!$('relatedGames') || AppState.selectedId !== id) return;
        const related = (result.items || []).map(item => ({...item, game:AppState.games.find(game => game.id === item.id)})).filter(item => item.game);
        $('relatedGames').innerHTML = related.length ? related.map(item => `<button class="related-game" data-related="${item.game.id}" title="${escapeHtml(item.reasons.join(', '))}">${escapeHtml(item.game.name)}<small>${escapeHtml(item.reasons.join(' · '))}</small></button>`).join('') : '<span class="description">No related games are in this library yet.</span>';
        document.querySelectorAll('[data-related]').forEach(button => button.onclick = () => selectGame(Number(button.dataset.related)));
      } catch(error) { if ($('relatedGames')) $('relatedGames').textContent = error.message; }
    }
    function render() { renderQueryChips(); renderPlatformCategories(); renderPlatforms(); renderPlaylists(); renderFilterPresets(); renderGrid(); renderDetails(); markFilterAria(); applyDetailsLayout(); applySidebarVisibility(); $('status').textContent = `${AppState.games.length} games · local library`; }
    function collectionStats(items) {
      const completed = items.filter(game => ['Beaten','Completed','Mastered'].includes(game.progress)).length;
      const playtime = items.reduce((total, game) => total + Number(game.playtime_seconds || 0), 0);
      const plays = items.reduce((total, game) => total + Number(game.play_count || 0), 0);
      return `<div class="collection-summary"><div class="collection-stat"><small>Games</small><strong>${items.length}</strong></div><div class="collection-stat"><small>Completed</small><strong>${completed}</strong></div><div class="collection-stat"><small>Play time</small><strong>${duration(playtime)}</strong></div><div class="collection-stat"><small>Launches</small><strong>${plays}</strong></div><div class="collection-stat"><small>Favorites</small><strong>${items.filter(game => game.favorite).length}</strong></div><div class="collection-stat"><small>Missing files</small><strong>${items.filter(game => !game.path_exists).length}</strong></div></div>`;
    }
    function renderCollectionDetails(title, items, kind) {
      const lastPlayed = [...items].filter(game => game.last_played).sort((a,b) => String(b.last_played).localeCompare(String(a.last_played)))[0];
      const mostPlayed = [...items].sort((a,b) => Number(b.play_count || 0) - Number(a.play_count || 0))[0];
      const random = items[Math.floor(Math.random() * items.length)];
      $('details').innerHTML = `<div class="platform-panel"><div class="hero-kicker">${escapeHtml(kind)}</div><h2>${escapeHtml(title)}</h2><p class="description">${items.length} game${items.length === 1 ? '' : 's'} in this view.</p>${collectionStats(items)}<div class="detail-card"><h3>Quick actions</h3><div class="extras">${random ? `<button class="primary" id="playRandomCollection">Play random</button>` : ''}${lastPlayed ? `<button class="icon-button" data-collection-game="${lastPlayed.id}">Last played · ${escapeHtml(lastPlayed.name)}</button>` : ''}${mostPlayed ? `<button class="icon-button" data-collection-game="${mostPlayed.id}">Most played · ${escapeHtml(mostPlayed.name)}</button>` : ''}</div></div><div class="detail-card"><h3>Featured games</h3><div class="related-grid">${items.slice(0,8).map(game => `<button class="related-game" data-collection-game="${game.id}" title="${escapeHtml(game.name)}">${escapeHtml(game.name)}</button>`).join('') || '<span class="description">No games found.</span>'}</div></div></div>`;
      if (random) $('playRandomCollection').onclick = () => launch(random.id);
      document.querySelectorAll('[data-collection-game]').forEach(button => button.onclick = () => selectGame(Number(button.dataset.collectionGame)));
    }
    async function renderPlatformDetails(platformName) {
      const count = AppState.games.filter(game => (game.platform || 'Unspecified') === platformName).length;
      const items = AppState.games.filter(game => (game.platform || 'Unspecified') === platformName);
      let documents = [];
      try {
        const result = await api(`/api/platform/documents?platform=${encodeURIComponent(platformName)}`);
        documents = result.documents || [];
      } catch(error) {
        documents = AppState.appSettings.platform_documents?.[platformName] || [];
      }
      $('details').innerHTML = `<div class="platform-panel"><div class="hero-kicker">Platform</div><h2>${escapeHtml(platformName)}</h2><p class="description">${count} game${count === 1 ? '' : 's'} in this platform view.</p>${collectionStats(items)}<div class="detail-card"><h3>Platform documents</h3>${documents.length ? `<div class="extras">${documents.map((item,index) => `<button class="icon-button" data-platform-doc="${index}">${escapeHtml(item.name)}</button>`).join('')}</div>` : '<p class="description">No platform documents configured yet.</p>'}<div class="extras"><button class="icon-button" id="editPlatformDocuments">Edit platform documents</button></div></div><div class="detail-card"><h3>Platform shortcuts</h3><div class="extras"><button class="primary" id="platformRandom">Play random</button>${items.filter(game => game.last_played).sort((a,b) => String(b.last_played).localeCompare(String(a.last_played)))[0] ? `<button class="icon-button" id="platformLast">Last played</button>` : ''}${items.sort((a,b) => Number(b.play_count || 0) - Number(a.play_count || 0))[0] ? `<button class="icon-button" id="platformMost">Most played</button>` : ''}</div></div></div>`;
      if (items.length) $('platformRandom').onclick = () => launch(items[Math.floor(Math.random() * items.length)].id);
      const last = items.filter(game => game.last_played).sort((a,b) => String(b.last_played).localeCompare(String(a.last_played)))[0];
      const most = [...items].sort((a,b) => Number(b.play_count || 0) - Number(a.play_count || 0))[0];
      if (last && $('platformLast')) $('platformLast').onclick = () => selectGame(last.id);
      if (most && $('platformMost')) $('platformMost').onclick = () => selectGame(most.id);
      $('editPlatformDocuments').onclick = async () => {
        const current = (AppState.appSettings.platform_documents?.[platformName] || documents).map(item => `${item.name} | ${item.path}`).join('\n');
        const value = await promptInput({
          title: `Platform documents for ${platformName}`,
          message: 'One per line: Name | Path',
          label: 'Documents',
          defaultValue: current,
        });
        if (value === null) return;
        const rows = value.split('\n').map(line => line.trim()).filter(Boolean).map(line => { const [name,...rest] = line.split('|'); return {name:(name || '').trim(), path:rest.join('|').trim()}; }).filter(item => item.name && item.path);
        try {
          await api('/api/platform/documents',{method:'POST',body:JSON.stringify({platform:platformName,documents:rows})});
          AppState.appSettings.platform_documents = {...(AppState.appSettings.platform_documents || {}), [platformName]: rows};
          renderPlatformDetails(platformName);
          notify('Platform documents saved');
        } catch(error) { notify(error.message); }
      };
      document.querySelectorAll('[data-platform-doc]').forEach(button => button.onclick = () => {
        const doc = documents[Number(button.dataset.platformDoc)];
        if (!doc) return;
        openReader({id:'platform', documents:[doc], name:platformName}, 0, `/api/platform/document?platform=${encodeURIComponent(platformName)}&index=${button.dataset.platformDoc}&token=${encodeURIComponent(token)}`);
      });
    }
    async function favorite(id) { try { const result = await api('/api/favorite',{method:'POST',body:JSON.stringify({id})}); const game = AppState.games.find(item => item.id === id); if (game) { game.favorite = result.favorite; AppState._refreshCounter = (AppState._refreshCounter || 0) + 1; } renderGrid(); renderDetails(); } catch(error) { notify(error.message); } }
    async function updateGameStatus(id, progress) {
      const game = AppState.games.find(item => item.id === id);
      if (!game) return;
      try { await api('/api/game',{method:'POST',body:JSON.stringify({id,game:{...game,progress}})}); await refresh(); notify(`Progress set to ${progress || 'Not set'}`); } catch(error) { notify(error.message); }
    }
    async function removeGame(id,name) {
      const ok = await confirmAction({
        title: 'Remove game',
        target: name,
        consequence: 'This game will be removed from OpenBox.',
        retained: 'Game files on disk are not deleted.',
        recovery: 'Re-import the folder or add the game again.',
        destructive: true,
        confirmLabel: 'Remove',
      });
      if (!ok) return;
      const alsoDeleteMedia = await confirmAction({
        title: 'Delete media files',
        target: name,
        consequence: 'Associated media files listed in this game entry will be deleted.',
        retained: 'ROM and save files outside the media list remain.',
        recovery: 'Re-download metadata media later.',
        destructive: true,
        confirmLabel: 'Delete media',
      });
      try { await api('/api/game/delete',{method:'POST',body:JSON.stringify({id,delete_media:alsoDeleteMedia})}); AppState.selectedId = null; await refresh(); notify('Game removed from library'); } catch(error) { notify(error.message); }
    }
    async function launchExtra(id,kind,index) { try { await api('/api/extra/launch',{method:'POST',body:JSON.stringify({id,kind,index})}); notify('Opened'); } catch(error) { notify(error.message); } }
    $('sidebarSearch').oninput = () => { leaveActivePreset(); AppState.activePlaylist = ''; scheduleSearch(() => { renderPlaylists(); renderGrid(); }); };
    $('view').onchange = () => { leaveActivePreset(); AppState.activePlaylist = ''; renderPlaylists(); renderGrid(); };
    $('sort').onchange = renderGrid;
    // load events do not bubble, so ratio tracking uses a capture-phase
    // listener on #grid that survives innerHTML rebuilds without re-attaching.
    $('grid').addEventListener('load', event => {
      const img = event.target;
      if (img.tagName === 'IMG' && img.dataset.gid) recordCoverRatio(img);
    }, true);
    $('grouping').onchange = async () => {
      AppState.appSettings.cover_grouping = $('grouping').value;
      if ($('groupingSetting')) $('groupingSetting').value = $('grouping').value;
      renderGrid();
      await api('/api/settings',{method:'POST',body:JSON.stringify({cover_grouping:AppState.appSettings.cover_grouping})}).catch(() => {});
    };
    if ($('esrbFilter')) $('esrbFilter').onchange = () => { leaveActivePreset(); renderGrid(); };
    const libraryPaneElement = document.querySelector('main.library');
    let scrollFramePending = false;
    if (libraryPaneElement) {
      // Coalesce raw scroll events into one render per animation frame: they
      // fire far faster than paint, and each renderGrid rewrites the whole
      // visible grid. The row-distance check drops renders that did not cross
      // a row boundary, and renders that do run skip the entrance animation.
      libraryPaneElement.addEventListener('scroll', () => {
        if (scrollFramePending) return;
        scrollFramePending = true;
        requestAnimationFrame(() => {
          scrollFramePending = false;
          const top = libraryPaneElement.scrollTop;
          if (gridRowHeight && Math.abs(top - gridScrollTop) < gridRowHeight) return;
          gridScrollTop = top;
          renderGrid({fromScroll:true});
        });
      }, { passive: true });
      window.addEventListener('resize', () => { gridRowHeight = 0; applyDetailsLayout(); renderGrid(); });
    }
    $('viewToggleButton').onclick = async () => {
      AppState.appSettings.library_view = (AppState.appSettings.library_view || 'grid') === 'list' ? 'grid' : 'list';
      if ($('libraryViewSetting')) $('libraryViewSetting').value = AppState.appSettings.library_view;
      applyLocaleStrings();
      renderGrid();
      await api('/api/settings',{method:'POST',body:JSON.stringify({library_view:AppState.appSettings.library_view})}).catch(() => {});
    };
    if ($('dropZone')) {
      ['dragenter','dragover'].forEach(name => $('dropZone').addEventListener(name, event => { event.preventDefault(); $('dropZone').classList.add('active'); }));
      $('dropZone').addEventListener('dragleave', () => $('dropZone').classList.remove('active'));
      $('dropZone').addEventListener('drop', async event => {
        event.preventDefault();
        $('dropZone').classList.remove('active');
        const folder = await nativePickFolder('Enter the absolute path of the folder to import.');
        if (folder) importDroppedFolder(folder.trim());
      });
      $('dropZone').onclick = () => importFolder();
    }
    bindDetailsResize();
    bindFilterDrawer();
    applyDetailsLayout();

export { refresh, render, renderGrid, renderDetails, renderPlaylists, renderFilterPresets, renderPlatformCategories, renderPlatforms, renderQueryChips, selectGame, favorite, updateGameStatus, removeGame, launchExtra, loadRelated, isVirtualEnabled, getSearchWorker, workerSearch, searchWithFallback, verifyWorkerParity, ensureVirtualObserver };
