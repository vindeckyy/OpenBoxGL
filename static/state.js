import { escapeHtml, API_V1, badge, defaultBadges, sortGames, advancedQueryMatches, parseQueryTokens, gameInstalled, $ } from './util.js';
import { render } from './library.js';



const token = new URLSearchParams(location.search).get('token') || '';
    /**
     * Central client application state container.
     * @type {Record<string, any>}
     */
    const AppState = {
      games: [], playlists: [], filterPresets: [], explorerField: 'genre', explorerRules: {}, activeFilterPreset: '', bigBoxGames: [], runningGames: [], raConfigured: false, selectedId: null, platform: 'all', activePlaylist: '', editingId: null, metadataGameId: null, bigBoxIndex: 0, gamepadState: {}, lastSessionEvent: 0, bulkMode: false, bigBoxLastInput: performance.now(), screenSaverGame: null, contextGameId: null, availableProfiles: {},
      appSettings: {watch_folders:[],screensaver_seconds:90,controller_map:{},library_view:'grid',cover_grouping:'shape',locale:'en'}, bigBoxFilter: 'all', bigBoxSort: 'title', bigBoxRaFilter: 'all', bigBoxPlatform: 'all', platformCategory: 'all', pendingUpdate: null, duplicateMediaGroups: 0, libraryBgm: null, readerPage: 1, readerUrl: '', bigBoxHybridQuery: '', mediaEpoch: 0, coverRatios: {},
    };

    const selectedIds = new Set();
    const media = (game, kind, index = '') => `/api/media?id=${game.id}&kind=${kind}${index === '' ? '' : `&index=${index}`}&v=${AppState.mediaEpoch}&token=${encodeURIComponent(token)}`;
    const badgeVisibility = () => new Set(AppState.appSettings.badge_visibility || defaultBadges);
    const playlistFor = name => AppState.playlists.find(item => item.name === name);
    const playlistMembers = playlist => new Set((playlist?.members || []).map(String));
    const gameInPlaylist = (game, playlist) => {
      if (!playlist) return false;
      if (playlist.type !== 'manual') return true;
      return playlistMembers(playlist).has(String(game.game_id)) || playlistMembers(playlist).has(String(game.id));
    };
    function renderBadges(game) {
      const visible = badgeVisibility();
      return [
        visible.has('favorite') && badge('Favorite', game.favorite, 'favorite'),
        visible.has('installed') && badge(gameInstalled(game) ? 'Installed' : 'Owned', true),
        visible.has('missing_media') && badge('Missing media', game.has_missing_media, 'danger'),
        visible.has('saves') && badge('Saves', game.has_saves),
        visible.has('documents') && badge('Docs', game.has_documents),
        visible.has('versions') && badge('Versions', game.has_versions),
        visible.has('storefront') && badge(game.source || 'Storefront', Boolean(game.source)),
        visible.has('achievements') && badge('Achievements', game.has_achievements),
        visible.has('highscores') && badge('High scores', game.has_highscores),
        visible.has('progress') && badge(game.progress, Boolean(game.progress), 'progress'),
        visible.has('rating') && badge(`${game.rating} stars`, Number(game.rating) > 0),
        visible.has('broken') && badge('Broken', game.broken, 'danger'),
        visible.has('portable') && badge('Portable', game.portable),
        visible.has('controller') && badge(game.controller_support, Boolean(game.controller_support)),
      ].filter(Boolean).join('');
    }
    /**
     * Perform an authenticated API request to the OpenBox backend.
     * @param {string} path
     * @param {RequestInit} [options]
     * @returns {Promise<any>}
     */
    async function api(path, options = {}) {
      // The v1 surface is the stable contract; unmapped call sites keep the
      // legacy paths until they are migrated one by one.
      const target = API_V1[path.replace(/^\/api\//, '').replace(/\//g, '_')] || path;
      let response;
      try {
        response = await fetch(target, { ...options, headers:{'X-OpenBox-Token':token,'Content-Type':'application/json',...(options.headers || {})} });
      } catch (error) {
        throw new Error(error.message || 'Could not reach the OpenBox server.');
      }
      const text = await response.text();
      let payload = {};
      if (text) {
        try { payload = JSON.parse(text); } catch { throw new Error('The server returned an invalid response.'); }
      }
      if (!response.ok) {
        const error = new Error(payload.error || 'Request failed');
        error.code = payload.code;
        error.requestId = payload.request_id;
        error.detail = payload.detail;
        throw error;
      }
      return payload;
    }
    const nativeBridge = typeof window.openboxNative === 'object' ? window.openboxNative : null;
    const nativeCaps = { webview:false, dialogs:false, tray:false, single_instance:false, gamepad:'webkit', fullscreen:true, clipboard:true };
    async function detectNative() {
      try {
        Object.assign(nativeCaps, await api('/api/native/capabilities'));
      } catch { /* no host connected; browser fallbacks stay active */ }
    }
    function nativeEnabled(feature) { return Boolean(nativeCaps[feature]); }
    function nativePrompt(message, defaultValue = '') {
      // The host has no text input; its native dialogs are file/folder pickers
      // only, so plain text prompts always stay browser-side.
      return prompt(message, defaultValue);
    }
    function nativeConfirm(message) {
      return confirm(message);
    }
    async function nativePickFolder(message) {
      if (nativeBridge?.dialog) {
        try {
          const result = await nativeBridge.dialog('folder', {title:message});
          return result?.path || null;
        } catch { return null; }
      }
      return prompt(message);
    }
    async function nativePickFile(message) {
      if (nativeBridge?.dialog) {
        try {
          const result = await nativeBridge.dialog('file', {title:message});
          return result?.path || null;
        } catch { return null; }
      }
      return prompt(message);
    }
    async function nativeReveal(path) {
      if (!path) return false;
      if (nativeBridge?.reveal) {
        try {
          const result = await nativeBridge.reveal(path);
          return Boolean(result?.ok);
        } catch { return false; }
      }
      try {
        const result = await api('/api/native/reveal',{method:'POST',body:JSON.stringify({path})});
        return Boolean(result?.ok);
      } catch { return false; }
    }
    async function nativeOpenExternal(target) {
      if (nativeBridge?.openExternal) { const result = await nativeBridge.openExternal(target); return result?.ok; }
      if (nativeEnabled('dialogs')) { const result = await api('/api/native/open-external',{method:'POST',body:JSON.stringify({url:target})}); return result?.ok; }
      window.open(target, '_blank');
      return true;
    }
    async function nativeWindowAction(action) {
      if (nativeBridge?.windowAction) { const result = await nativeBridge.windowAction(action); return result?.ok; }
      if (nativeEnabled('fullscreen')) {
        const result = await api('/api/native/window',{method:'POST',body:JSON.stringify({action})});
        return result?.ok;
      }
      return false;
    }
    // The native host fullscreens the GTK window, which never sets
    // document.fullscreenElement, so the toggle state is tracked here.
    let nativeFullscreenOn = false;
    async function nativeFullscreen() {
      if (nativeBridge?.windowAction) {
        const action = nativeFullscreenOn ? 'unset-fullscreen' : 'set-fullscreen';
        const result = await nativeBridge.windowAction(action);
        if (result?.ok !== false) nativeFullscreenOn = !nativeFullscreenOn;
        return result;
      }
      return document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
    }
    function notify(message) { $('toast').textContent = message; $('toast').classList.add('show'); clearTimeout(notify.timer); notify.timer = setTimeout(() => $('toast').classList.remove('show'), 2800); }
    let lastBannerDetails = '';
    function showErrorBanner(error) {
      const text = error?.message || String(error || 'Something went wrong.');
      $('errorBannerText').textContent = text;
      lastBannerDetails = [
        text,
        error?.code ? `code: ${error.code}` : '',
        error?.requestId ? `request id: ${error.requestId}` : '',
        error?.detail ? String(error.detail) : '',
      ].filter(Boolean).join('\n');
      $('errorBanner').hidden = false;
      clearTimeout(showErrorBanner.timer);
      showErrorBanner.timer = setTimeout(() => { $('errorBanner').hidden = true; }, 10000);
    }
    function copyDiagnostics() {
      try {
        navigator.clipboard.writeText(lastBannerDetails || 'No error details captured.');
        notify('Error details copied');
      } catch (error) {
        notify('Could not copy: clipboard unavailable');
      }
    }
    $('errorBannerDismiss').onclick = () => { $('errorBanner').hidden = true; };
    $('errorBannerCopy').onclick = copyDiagnostics;
    window.addEventListener('unhandledrejection', event => {
      if (event.reason && (event.reason.code === 'INTERNAL_ERROR' || event.reason.requestId || event.reason instanceof Error)) {
        showErrorBanner(event.reason);
      }
    });
    function setButtonBusy(button, busy) {
      if (!button) return;
      button.disabled = busy;
      button.toggleAttribute('aria-busy', busy);
      if (busy) button.setAttribute('aria-label', 'Starting game');
      else button.removeAttribute('aria-label');
    }
    let profilesFetched = false;
    async function ensureProfiles() {
      if (profilesFetched) return;
      profilesFetched = true;
      try { AppState.availableProfiles = (await api('/api/profiles')).profiles || {}; } catch(error) { profilesFetched = false; }
    }
    function applyLocaleStrings() {
      const strings = AppState.appSettings.strings || {};
      if (strings.drop_import && $('dropZone')) $('dropZone').textContent = strings.drop_import;
      if ($('viewToggleButton')) {
        const listView = (AppState.appSettings.library_view || 'grid') === 'list';
        $('viewToggleButton').textContent = listView ? (strings.grid_view || 'Grid view') : (strings.list_view || 'List view');
      }
    }
    function platformCategoryFor(game) {
      const categories = AppState.appSettings.platform_categories || {};
      return categories[game.platform] || 'Other';
    }
    function applySidebarVisibility() {
      const hidden = new Set((AppState.appSettings.hidden_sidebar_sections || []).map(value => value.trim().toLowerCase()).filter(Boolean));
      document.querySelectorAll('[data-sidebar-section]').forEach(element => {
        element.hidden = hidden.has(element.dataset.sidebarSection);
      });
    }
    const SEARCH_INDEX_MAX_TERM = 32;
    // LRU cache for parsed query tokens — caps at 64 entries to bound memory.
    const QUERY_TOKEN_CACHE_MAX = 64;
    const _queryTokenCache = new Map();
    function cachedParseQueryTokens(query) {
      let tokens = _queryTokenCache.get(query);
      if (tokens !== undefined) { _queryTokenCache.delete(query); _queryTokenCache.set(query, tokens); return tokens; }
      tokens = parseQueryTokens(query);
      _queryTokenCache.set(query, tokens);
      if (_queryTokenCache.size > QUERY_TOKEN_CACHE_MAX) { const oldest = _queryTokenCache.keys().next().value; _queryTokenCache.delete(oldest); }
      return tokens;
    }
    // Abortable debounce for search — callers pass a callback; prior pending
    // timeout is cancelled so only the last invocation within the delay fires.
    let _searchTimer = null;
    function scheduleSearch(callback, delay = 150) { clearTimeout(_searchTimer); _searchTimer = setTimeout(callback, delay); }
    let _searchIndex = { games: null, refresh: null, title: new Map(), all: [] };
    let _searchIndexDirty = true;
    function markSearchIndexDirty() { _searchIndexDirty = true; }
    let _filterVersion = 0;
    function invalidateFilterCache() { _filterVersion++; }
    function indexValues(game) {
      return [game.name, game.sort_title, ...(Array.isArray(game.alternate_names) ? game.alternate_names : [game.alternate_names])]
        .filter(value => value !== undefined && value !== null && value !== '')
        .map(value => String(value).toLowerCase());
    }
    function indexTerms(values) {
      const terms = new Set();
      values.forEach(value => {
        (value.match(/[a-z0-9]+/g) || []).forEach(word => {
          const limited = word.slice(0, SEARCH_INDEX_MAX_TERM);
          // Prefixes: up to 8 substrings anchored at the start (lengths 2–9).
          for (let end = 2; end <= Math.min(limited.length, 9); end++) terms.add(limited.slice(0, end));
          // Suffixes: up to 8 substrings anchored at the end (lengths 2–9).
          for (let len = 2; len <= Math.min(limited.length, 9); len++) terms.add(limited.slice(limited.length - len));
          // 2-grams: every bigram for short-token matching.
          for (let i = 0; i <= limited.length - 2; i++) terms.add(limited.slice(i, i + 2));
        });
        const words = value.match(/[a-z0-9]+/g) || [];
        if (words.length > 1) terms.add(words.map(word => word[0]).join(''));
        if (words.length > 2 && ['the', 'a', 'an'].includes(words[0])) terms.add(words.slice(1).map(word => word[0]).join(''));
      });
      return terms;
    }
    function buildSearchIndex() {
      const refresh = AppState._refreshCounter || 0;
      if (_searchIndex.games === AppState.games && _searchIndex.refresh === refresh) return _searchIndex;
      const title = new Map();
      AppState.games.forEach(game => {
        for (const term of indexTerms(indexValues(game))) {
          let bucket = title.get(term);
          if (!bucket) title.set(term, bucket = []);
          bucket.push(game);
        }
      });
      _searchIndex = { games: AppState.games, refresh, title, all: AppState.games };
      AppState.searchIndexStats = { terms: title.size, games: AppState.games.length };
      _searchIndexDirty = false;
      return _searchIndex;
    }
    function indexedTitleCandidates(query) {
      if (!query || /[:"]/.test(query)) return null;
      const tokens = cachedParseQueryTokens(query);
      if (!tokens.length || tokens.some(token => token.negative || token.key !== 'title' || token.value.length < 2 || !/^[a-z0-9]+$/.test(token.value))) return null;
      const index = buildSearchIndex();
      let ids = null;
      for (const token of tokens) {
        const bucket = index.title.get(token.value.slice(0, SEARCH_INDEX_MAX_TERM)) || [];
        const next = new Set(bucket.map(game => game.id));
        ids = ids === null ? next : new Set([...ids].filter(id => next.has(id)));
        if (!ids.size) break;
      }
      return ids.size ? index.all.filter(game => ids.has(game.id)) : [];
    }
    function warmSearchIndex() {
      buildSearchIndex();
    }
    let _filteredCache = { key: null, result: [] };
    /**
     * Compute the filtered and sorted list of games for the active view.
     * @returns {Array<Record<string, any>>}
     */
    function filteredGames() {
      const query = ($('sidebarSearch')?.value || '').toLowerCase().trim();
      const view = $('view')?.value || 'all';
      const sort = $('sort')?.value || 'name';
      const esrb = $('esrbFilter')?.value || '';
      const key = `${_filterVersion}\0${AppState._refreshCounter || 0}\0${query}\0${view}\0${sort}\0${esrb}\0${AppState.platform}\0${AppState.platformCategory}\0${AppState.activePlaylist}\0${AppState.activeFilterPreset}\0${JSON.stringify(AppState.explorerRules)}`;
      const preset = AppState.filterPresets.find(item => item.name === AppState.activeFilterPreset);
      const presetRules = preset?.rules || {};
      const activePlaylistData = playlistFor(AppState.activePlaylist);
      const indexQuery = (presetRules.query || query).trim();
      const sourceGames = indexedTitleCandidates(indexQuery) || AppState.games;
      const visible = sourceGames.filter(game => {
        const completed = ['Beaten','Completed','Mastered'].includes(game.progress);
        const installed = gameInstalled(game);
        const ownedUninstalled = Boolean(game.owned || game.store_catalog || game.gameyfin_id) && !installed;
        const effectiveView = presetRules.view || view;
        const viewMatch = (effectiveView === 'all' && !game.hidden) || (effectiveView === 'favorites' && game.favorite && !game.hidden) || (effectiveView === 'recent' && game.last_played && !game.hidden) || (effectiveView === 'never' && !game.play_count && !game.hidden) || (effectiveView === 'playing' && ['Playing','Paused'].includes(game.progress) && !game.hidden) || (effectiveView === 'completed' && completed && !game.hidden) || (effectiveView === 'installed' && installed && !game.hidden) || (effectiveView === 'owned' && ownedUninstalled && !game.hidden) || (effectiveView === 'saves' && game.has_saves && !game.hidden) || (effectiveView === 'hidden' && game.hidden) || (effectiveView === 'missing' && !game.path_exists && !game.hidden);
        const effectivePlatform = presetRules.platform || AppState.platform;
        const platformMatch = effectivePlatform === 'all' || game.platform === effectivePlatform;
        const category = presetRules.platform_category || AppState.platformCategory;
        const categoryMatch = category === 'all' || platformCategoryFor(game) === category;
        const esrb = presetRules.esrb || $('esrbFilter')?.value || '';
        const esrbMatch = !esrb || (game.esrb || 'Unrated') === esrb;
        const effectiveQuery = (presetRules.query || query).trim();
        const queryMatch = !effectiveQuery || advancedQueryMatches(game, effectiveQuery);
        const progressMatch = !presetRules.progress || game.progress === presetRules.progress;
        const favoriteMatch = presetRules.favorite === undefined || Boolean(game.favorite) === Boolean(presetRules.favorite);
        const genreMatch = !presetRules.genre || String(game.genre || '').toLowerCase().includes(String(presetRules.genre).toLowerCase());
        const developerMatch = !presetRules.developer || String(game.developer || '').toLowerCase().includes(String(presetRules.developer).toLowerCase());
        const publisherMatch = !presetRules.publisher || String(game.publisher || '').toLowerCase().includes(String(presetRules.publisher).toLowerCase());
        const explorerProgressMatch = AppState.explorerRules.progress === '__unset' ? !game.progress : !AppState.explorerRules.progress || game.progress === AppState.explorerRules.progress;
        const installedRule = presetRules.installed;
        const installedMatch = installedRule !== 'installed' && installedRule !== 'uninstalled'
          || installedRule === 'installed' && installed
          || installedRule === 'uninstalled' && !installed;
        const hiddenMatch = presetRules.hidden === undefined || Boolean(game.hidden) === Boolean(presetRules.hidden);
        const playlistMatch = !activePlaylistData || gameInPlaylist(game, activePlaylistData);
        return viewMatch && platformMatch && categoryMatch && esrbMatch && queryMatch && progressMatch && explorerProgressMatch && favoriteMatch && genreMatch && developerMatch && publisherMatch && installedMatch && hiddenMatch && playlistMatch;
      });
      const sorted = sortGames(visible, sort);
      _filteredCache = { key, result: sorted };
      return sorted;
    }
    async function loadExplorerFacets(field = AppState.explorerField) {
      AppState.explorerField = field;
      const container = $('explorerFacets');
      if (!container) return;
      try {
        const result = await api(`/api/explorer/facets?field=${encodeURIComponent(field)}`);
        const tabs = ['genre','developer','platform','progress','esrb'].map(name => `<button type="button" class="platform ${AppState.explorerField === name ? 'active' : ''}" data-explorer-field="${name}">${name}</button>`).join('');
        const facets = (result.facets || []).map(item => `<button type="button" class="platform" data-explorer-value="${escapeHtml(item.value)}" data-explorer-field="${AppState.explorerField}">${escapeHtml(item.value)} (${item.count})</button>`).join('');
        container.innerHTML = `<div class="platforms">${tabs}</div><div class="platforms">${facets || '<span class="description">No values yet.</span>'}</div>`;
        document.querySelectorAll('[data-explorer-field]').forEach(button => {
          if (button.dataset.explorerValue) {
            button.onclick = () => {
              AppState.activeFilterPreset = '';
              AppState.activePlaylist = '';
              AppState.explorerRules = {};
              if (button.dataset.explorerField === 'genre') $('sidebarSearch').value = button.dataset.explorerValue;
              else if (button.dataset.explorerField === 'developer') $('sidebarSearch').value = button.dataset.explorerValue;
              else if (button.dataset.explorerField === 'platform') AppState.platform = button.dataset.explorerValue;
              else if (button.dataset.explorerField === 'progress') AppState.explorerRules = {progress:button.dataset.explorerValue === 'Unset' ? '__unset' : button.dataset.explorerValue};
              else if (button.dataset.explorerField === 'esrb' && $('esrbFilter')) $('esrbFilter').value = button.dataset.explorerValue === 'Unrated' ? '' : button.dataset.explorerValue;
              render();
            };
          } else {
            button.onclick = () => loadExplorerFacets(button.dataset.explorerField);
          }
        });
      } catch(error) {
        container.innerHTML = `<span class="description">${escapeHtml(error.message)}</span>`;
      }
    }

export { token, AppState, selectedIds, media, badgeVisibility, playlistFor, playlistMembers, gameInPlaylist, renderBadges, api, nativeBridge, detectNative, nativeEnabled, nativePrompt, nativeConfirm, nativePickFolder, nativePickFile, nativeReveal, nativeOpenExternal, nativeWindowAction, nativeFullscreenOn, nativeFullscreen, notify, lastBannerDetails, showErrorBanner, copyDiagnostics, setButtonBusy, profilesFetched, ensureProfiles, applyLocaleStrings, applySidebarVisibility, platformCategoryFor, filteredGames, warmSearchIndex, loadExplorerFacets, invalidateFilterCache, markSearchIndexDirty, scheduleSearch };
