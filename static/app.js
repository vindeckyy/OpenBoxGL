const token = new URLSearchParams(location.search).get('token') || '';
    const AppState = {
      games: [], playlists: [], filterPresets: [], explorerField: 'genre', explorerRules: {}, activeFilterPreset: '', bigBoxGames: [], runningGames: [], raConfigured: false, selectedId: null, platform: 'all', activePlaylist: '', editingId: null, metadataGameId: null, bigBoxIndex: 0, gamepadState: {}, lastSessionEvent: 0, bulkMode: false, bigBoxLastInput: performance.now(), screenSaverGame: null, contextGameId: null, availableProfiles: {},
      appSettings: {watch_folders:[],screensaver_seconds:90,controller_map:{},library_view:'grid',locale:'en'}, bigBoxFilter: 'all', bigBoxSort: 'title', bigBoxRaFilter: 'all', bigBoxPlatform: 'all', platformCategory: 'all', pendingUpdate: null, duplicateMediaGroups: 0, libraryBgm: null, readerPage: 1, readerUrl: '', bigBoxHybridQuery: '', mediaEpoch: 0,
      // Browser-only preferences survive reloads via localStorage; library
      // data stays server-owned and never enters this key.
      persist() {
        try {
          localStorage.setItem('openbox-ui-state-v1', JSON.stringify({
            library_view: this.appSettings.library_view,
            image_group: this.appSettings.image_group,
            badge_visibility: this.appSettings.badge_visibility,
            hidden_sidebar_sections: this.appSettings.hidden_sidebar_sections,
          }));
        } catch (error) { /* private mode or quota; ignore */ }
      },
      restorePersisted() {
        try {
          const saved = JSON.parse(localStorage.getItem('openbox-ui-state-v1') || '{}');
          for (const key of Object.keys(saved)) {
            if (saved[key] !== undefined && saved[key] !== null) this.appSettings[key] = saved[key];
          }
        } catch (error) { /* corrupted or unavailable; ignore */ }
      },
    };

    const defaultControllerMap = {play:0,back:1,favorite:2,random:3,page_left:4,page_right:5,pause:8,menu:9};
    const selectedIds = new Set();
    const $ = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const media = (game, kind, index = '') => `/api/media?id=${game.id}&kind=${kind}${index === '' ? '' : `&index=${index}`}&v=${AppState.mediaEpoch}&token=${encodeURIComponent(token)}`;
    const duration = seconds => { const minutes = Math.floor((seconds || 0) / 60), hours = Math.floor(minutes / 60); return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`; };
    const defaultBadges = ['favorite','installed','saves','documents','progress','storefront','achievements','rating'];
    const badgeVisibility = () => new Set(AppState.appSettings.badge_visibility || defaultBadges);
    const gameInstalled = game => game.store_installed !== false && (game.path_exists || game.store_installed);
    const playlistFor = name => AppState.playlists.find(item => item.name === name);
    const playlistMembers = playlist => new Set((playlist?.members || []).map(String));
    const gameInPlaylist = (game, playlist) => {
      if (!playlist) return false;
      if (playlist.type !== 'manual') return true;
      return playlistMembers(playlist).has(String(game.game_id)) || playlistMembers(playlist).has(String(game.id));
    };
    const formatBytes = value => { const bytes = Number(value || 0); return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`; };
    const queryTokenCache = new Map();
    function parseQueryTokens(query) {
      const cached = queryTokenCache.get(query);
      if (cached) return cached;
      const tokens = String(query || '').match(/(?:[^\s"]+:"[^"]*"|[^\s]+|"[^"]*")+/g) || [];
      const parsed = tokens.map(token => {
        const negative = token.startsWith('-');
        const raw = negative ? token.slice(1) : token;
        const separator = raw.indexOf(':');
        const key = separator > 0 ? raw.slice(0, separator).toLowerCase() : 'title';
        const value = (separator > 0 ? raw.slice(separator + 1) : raw).replace(/^"|"$/g, '').toLowerCase();
        return { negative, key, value };
      });
      if (queryTokenCache.size > 64) queryTokenCache.clear();
      queryTokenCache.set(query, parsed);
      return parsed;
    }
    function advancedQueryMatches(game, query) {
      const parsedTokens = parseQueryTokens(query);
      const fields = {
        title:['name','sort_title','alternate_names'], platform:['platform'], plat:['platform'], genre:['genre'],
        dev:['developer'], developer:['developer'], pub:['publisher'], publisher:['publisher'], series:['series'],
        region:['region'], play:['play_mode'], playmode:['play_mode'], notes:['notes'], source:['source'],
        store:['source'], storefront:['source'], status:['status'], progress:['progress'], rating:['rating'],
        favorite:['favorite'], fav:['favorite'], installed:['installed'], hide:['hidden'], hidden:['hidden'],
        broken:['broken'], portable:['portable'], controller:['controller_support'], tag:['tags'], tags:['tags'],
        all:['name','sort_title','alternate_names','platform','genre','developer','publisher','series','region','notes','source','play_mode','status','progress','controller_support','tags']
      };
      return parsedTokens.every(({ negative, key, value }) => {
        const names = fields[key] || fields.all;
        const values = names.flatMap(name => Array.isArray(game[name]) ? game[name] : [game[name]]).filter(item => item !== undefined && item !== null && item !== '');
        if (key === 'installed') values.push(gameInstalled(game) ? 'yes' : 'no');
        if (key === 'favorite' || key === 'fav') values.push(game.favorite ? 'yes' : 'no');
        if (key === 'hide' || key === 'hidden') values.push(game.hidden ? 'yes' : 'no');
        if (key === 'broken') values.push(game.broken ? 'yes' : 'no');
        if (key === 'portable') values.push(game.portable ? 'yes' : 'no');
        const matched = values.map(item => String(item).toLowerCase()).some(item => item.includes(value));
        return negative ? !matched : matched;
      });
    }
    function badge(label, value, kind = '') { return value ? `<span class="badge ${kind}" title="${escapeHtml(label)}">${escapeHtml(label)}</span>` : ''; }
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
    const artworkKinds = [['clear_logo','Clear logo','has_clear_logo'],['fanart','Fanart','has_fanart'],['banner','Banner','has_banner'],['icon','Icon','has_icon'],['box_back','Box back','has_box_back'],['box_spine','Box spine','has_box_spine'],['box_3d','3D box','has_box_3d'],['title_screen','Title screen','has_title_screen'],['cart_front','Cart front','has_cart_front'],['cart_back','Cart back','has_cart_back'],['disc','Disc','has_disc'],['advertisement','Advertisement / flyer','has_advertisement'],['manual','Manual','has_manual']];
    function renderArtwork(game) {
      const items = artworkKinds.filter(([, , flag]) => game[flag]);
      return items.length ? `<div class="detail-card"><h3>Artwork</h3><div class="screenshot-grid">${items.map(([kind,label]) => kind === 'manual' ? `<button data-manual="${media(game,'manual')}" aria-label="Open ${escapeHtml(label)}"><div class="cover-title">${escapeHtml(label)}</div></button>` : `<button data-artwork="${kind}" aria-label="Open ${escapeHtml(label)}"><img src="${media(game,kind)}" alt="${escapeHtml(label)}" loading="lazy" decoding="async"></button>`).join('')}</div></div>` : '';
    }

    const API_V1 = {
      library: '/api/v1/library', settings: '/api/v1/settings', health: '/api/v1/health',
      health_dedupe: '/api/v1/health/dedupe', launch: '/api/v1/launch', game: '/api/v1/game',
      game_delete: '/api/v1/game/delete', games_bulk: '/api/v1/games/bulk', queue: '/api/v1/queue',
      tags: '/api/v1/tags', notifications: '/api/v1/notifications', webhooks: '/api/v1/webhooks',
      playlists: '/api/v1/playlists', running: '/api/v1/running', history: '/api/v1/history',
      saves: '/api/v1/saves', media: '/api/v1/media', media_bulk: '/api/v1/media/bulk',
      media_audit: '/api/v1/media/audit', metadata_status: '/api/v1/metadata/status',
      metadata_apply: '/api/v1/metadata/apply', metadata_search: '/api/v1/metadata/search',
      import: '/api/v1/import', import_steam: '/api/v1/import/steam',
      import_heroic: '/api/v1/import/heroic', import_lutris: '/api/v1/import/lutris',
      import_arcade: '/api/v1/import/arcade', emulators: '/api/v1/emulators',
      emulators_install: '/api/v1/emulators/install', profiles: '/api/v1/profiles',
      themes: '/api/v1/themes', update: '/api/v1/update', update_install: '/api/v1/update/install',
      backup: '/api/v1/backup', backup_create: '/api/v1/backup/create',
      backup_restore: '/api/v1/backup/restore', backups: '/api/v1/backups', jobs: '/api/v1/jobs',
      log: '/api/v1/log', diagnostic: '/api/v1/diagnostic', shutdown: '/api/v1/shutdown',
      favorite: '/api/v1/favorite', plugins: '/api/v1/plugins', state_recover: '/api/v1/state/recover',
      filter_presets: '/api/v1/filter-presets',
    };
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
    async function refresh() {
      const state = await api('/api/library');
      AppState.games = state.games;
      AppState.playlists = state.playlists || [];
      AppState.filterPresets = state.filter_presets || state.settings?.filter_presets || AppState.appSettings.filter_presets || [];
      AppState.raConfigured = state.ra_configured;
      AppState.appSettings = state.settings || AppState.appSettings;
      AppState.mediaEpoch = state.media_epoch || 0;
      if (AppState.activePlaylist && !AppState.playlists.some(item => item.name === AppState.activePlaylist)) AppState.activePlaylist = '';
      if (AppState.selectedId !== null && !AppState.games.some(game => game.id === AppState.selectedId)) AppState.selectedId = null;
      for (const id of selectedIds) if (!AppState.games.some(game => game.id === id)) selectedIds.delete(id);
      render();
      applySidebarVisibility();
      applyLocaleStrings();
      maybeShowWelcome();
      const fingerprint = `${AppState.games.length}:${AppState.games[0]?.id || ''}:${AppState.games.at(-1)?.id || ''}`;
      if (lastFacetsFingerprint !== fingerprint) {
        lastFacetsFingerprint = fingerprint;
        loadExplorerFacets().catch(() => {});
      }
    }
    let profilesFetched = false, lastFacetsFingerprint = null;
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
    function recentActivityValue(game) {
      const played = Date.parse(game.last_played || '') || 0;
      const added = Date.parse(game.added_at || '') || 0;
      return Math.max(played, added);
    }
    function sortGames(list, sort) {
      return list.sort((a, b) => sort === 'rating' ? Number(b.rating || 0) - Number(a.rating || 0) || a.name.localeCompare(b.name)
        : sort === 'recent' ? String(b.last_played || '').localeCompare(String(a.last_played || ''))
        : sort === 'recent_activity' ? recentActivityValue(b) - recentActivityValue(a) || a.name.localeCompare(b.name)
        : sort === 'playtime' ? Number(b.playtime_seconds || 0) - Number(a.playtime_seconds || 0)
        : sort === 'added' ? String(b.added_at || '').localeCompare(String(a.added_at || ''))
        : sort === 'platform' ? String(a.platform || '').localeCompare(String(b.platform || '')) || String(a.sort_title || a.name).localeCompare(String(b.sort_title || b.name))
        : sort === 'genre' ? String(a.genre || '').localeCompare(String(b.genre || '')) || String(a.sort_title || a.name).localeCompare(String(b.sort_title || b.name))
        : String(a.sort_title || a.name).localeCompare(String(b.sort_title || b.name)));
    }
    function filteredGames() {
      const query = $('sidebarSearch').value.toLowerCase().trim();
      const view = $('view').value;
      const preset = AppState.filterPresets.find(item => item.name === AppState.activeFilterPreset);
      const presetRules = preset?.rules || {};
      const activePlaylistData = playlistFor(AppState.activePlaylist);
      const visible = AppState.games.filter(game => {
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
      return sortGames(visible, $('sort').value);
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
      document.querySelectorAll('[data-platform-category]').forEach(button => button.onclick = () => { AppState.activePlaylist = ''; AppState.platformCategory = button.dataset.platformCategory; AppState.selectedId = null; render(); loadTheme(); });
    }
    function renderPlatforms() {
      const counts = new Map();
      AppState.games.forEach(game => counts.set(game.platform || 'Unspecified', (counts.get(game.platform || 'Unspecified') || 0) + 1));
      const items = [['all',`All (${AppState.games.length})`], ...[...counts].sort((a,b) => a[0].localeCompare(b[0])).map(([name,count]) => [name,`${name} (${count})`])];
      $('platforms').innerHTML = items.map(([value,label]) => `<button class="platform ${AppState.platform === value ? 'active' : ''}" data-platform="${escapeHtml(value)}">${escapeHtml(label)}</button>`).join('');
      document.querySelectorAll('[data-platform]').forEach(button => button.onclick = () => { AppState.activePlaylist = ''; AppState.platform = button.dataset.platform; AppState.selectedId = null; render(); loadTheme(); });
    }
    function renderPlaylists() {
      $('playlists').innerHTML = AppState.playlists.length ? AppState.playlists.map(item => `<div class="playlist-row"><button class="platform ${AppState.activePlaylist === item.name ? 'active' : ''}" data-playlist="${escapeHtml(item.name)}">${escapeHtml(item.name)} <small>${item.type === 'manual' ? `(${(item.members || []).length})` : '↻'}</small></button><button class="playlist-delete" data-delete-playlist="${escapeHtml(item.name)}" aria-label="Delete ${escapeHtml(item.name)}">×</button></div>`).join('') : '<span class="description">Create a playlist to pin one here.</span>';
      document.querySelectorAll('[data-playlist]').forEach(button => button.onclick = () => {
        const item = AppState.playlists.find(playlist => playlist.name === button.dataset.playlist);
        if (!item) return;
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
        if (!confirm(`Delete preset "${button.dataset.deletePreset}"?`)) return;
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
    async function loadExplorerFacets(field = explorerField) {
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
    function selectGame(id) {
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
      if (available) {
        const index = imageGroup === 'screenshot' ? game.available_screenshots[0] : '';
        return `<img src="${media(game,imageGroup,index)}" alt="" loading="lazy" decoding="async">`;
      }
      return `<div class="cover-title">${escapeHtml(game.name)}</div>`;
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
      // Grid rows are sized by their tallest card. Natural-ratio covers make
      // card heights vary, so use the tallest card in the first row instead of
      // a single-card sample, otherwise the virtual window drifts on scroll.
      let rowHeight = 0;
      for (let i = 0; i < Math.min(gridCols, cards.length); i++) rowHeight = Math.max(rowHeight, cards[i].offsetHeight);
      gridRowHeight = rowHeight + rowGap;
    }
    function gridWindow(total) {
      if (!gridRowHeight) return [0, total];
      const pane = gridPane();
      const paneHeight = pane ? pane.clientHeight : 0;
      const rows = Math.ceil(total / Math.max(gridCols, 1));
      const firstRow = Math.max(0, Math.floor(gridScrollTop / gridRowHeight) - 2);
      const lastRow = Math.min(rows - 1, Math.ceil((gridScrollTop + paneHeight) / gridRowHeight) + 2);
      return [Math.min(total, firstRow * gridCols), Math.min(total, (lastRow + 1) * gridCols)];
    }
    function renderGrid() {
      const pane = gridPane();
      if (pane) gridScrollTop = pane.scrollTop;
      const visible = filteredGames();
      const explicitImageGroup = AppState.activePlaylist ? AppState.appSettings.image_group_by_playlist?.[AppState.activePlaylist] : AppState.platform !== 'all' ? AppState.appSettings.image_group_by_platform?.[AppState.platform] : AppState.appSettings.image_group;
      $('imageGroup').value = explicitImageGroup || (AppState.platform === 'all' && !AppState.activePlaylist ? 'cover' : 'default');
      const imageGroup = $('imageGroup').value === 'default' ? AppState.appSettings.image_group || 'cover' : $('imageGroup').value;
      $('bulkButton').textContent = AppState.bulkMode ? selectedIds.size ? `Edit ${selectedIds.size} Selected` : 'Cancel Bulk Edit' : 'Bulk Edit';
      $('libraryTitle').textContent = AppState.activeFilterPreset || AppState.activePlaylist || (AppState.platform === 'all' ? $('view').selectedOptions[0].text : AppState.platform);
      $('libraryMeta').textContent = AppState.bulkMode ? `${selectedIds.size} selected · ${visible.length} shown` : `${visible.length} game${visible.length === 1 ? '' : 's'}`;
      $('surpriseButton').disabled = !visible.length;
      $('status').textContent = `${AppState.games.length} games · local library`;
      if (!visible.length) {
        $('grid').className = 'grid';
        $('grid').innerHTML = AppState.games.length
          ? `<div class="empty"><div><h2>No games match this view</h2><p>Change the active filters or search the library again.</p></div></div>`
          : `<div class="empty"><div><h2>Start your library</h2><p>Bring your games into OpenBox, then search, filter, and launch them from one collection.</p><div class="empty-actions"><button id="emptyAdd">Add game</button><button class="empty-secondary" id="emptyImport">Import folder</button><button class="empty-secondary" id="emptySteam">Import Steam</button></div></div></div>`;
        if ($('emptyAdd')) $('emptyAdd').onclick = () => openGameDialog();
        if ($('emptyImport')) $('emptyImport').onclick = () => importFolder();
        if ($('emptySteam')) $('emptySteam').onclick = () => importSteam();
        gridRowHeight = 0;
        renderArrangeBar(visible);
        return;
      }
      const listView = (AppState.appSettings.library_view || 'grid') === 'list';
      $('grid').className = listView ? 'list-view' : 'grid';
      const total = visible.length;
      const [start, end] = gridWindow(total);
      const rows = Math.ceil(total / Math.max(gridCols, 1));
      const topHeight = Math.floor(start / Math.max(gridCols, 1)) * gridRowHeight;
      const bottomHeight = gridRowHeight ? Math.max(0, rows - Math.ceil(end / Math.max(gridCols, 1))) * gridRowHeight : 0;
      const topSpacer = gridRowHeight ? `<div class="grid-spacer" style="height:${topHeight}px"></div>` : '';
      const bottomSpacer = gridRowHeight ? `<div class="grid-spacer" style="height:${bottomHeight}px"></div>` : '';
      const chunk = visible.slice(start, end);
      const rendered = chunk.map((game,index) => listView
         ? `<button type="button" class="list-row motion-enter ${AppState.selectedId === game.id || selectedIds.has(game.id) ? 'selected' : ''}" style="--motion-index:${Math.min(index,10)}" data-game="${game.id}" aria-label="Open ${escapeHtml(game.name)}"><strong>${escapeHtml(game.name)}<span class="badge-row">${renderBadges(game)}</span></strong><span>${escapeHtml(game.platform || '')}</span><span>${escapeHtml(game.genre || '')}</span><span>${escapeHtml(game.esrb || '-')}</span><span>${escapeHtml(game.progress || '')}</span><span>${game.play_count || 0}</span><span>${game.rating || ''}</span></button>`
         : `<article class="card motion-enter ${AppState.selectedId === game.id || selectedIds.has(game.id) ? 'selected' : ''}" style="--motion-index:${Math.min(index,10)}">
        ${AppState.bulkMode ? `<input class="card-picker" type="checkbox" data-game-picker="${game.id}" ${selectedIds.has(game.id) ? 'checked' : ''} aria-label="Select ${escapeHtml(game.name)}">` : ''}
        <button type="button" class="card-main" data-game="${game.id}" aria-label="Open ${escapeHtml(game.name)}"><div class="cover ${AppState.appSettings.bigbox_mode === 'coverflow' ? 'jewel-3d' : ''}">${imageMarkup(game,imageGroup)}</div>
        <h3>${escapeHtml(game.name)}</h3><p>${escapeHtml(game.developer || game.platform || '')}</p>
        <div class="badge-row">${renderBadges(game)}</div></button>
      </article>`).join('');
      $('grid').innerHTML = listView ? `<div class="list-head"><span>Title</span><span>Platform</span><span>Genre</span><span>ESRB</span><span>Progress</span><span>Plays</span><span>Rating</span></div>${topSpacer}${rendered}${bottomSpacer}` : `${topSpacer}${rendered}${bottomSpacer}`;
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
      renderArrangeBar(visible);
      const measuredBefore = gridRowHeight;
      measureGridLayout();
      if (!measuredBefore && gridRowHeight && total) renderGrid();
    }
    function renderDetails() {
      const game = AppState.games.find(item => item.id === AppState.selectedId);
      if (!game && (AppState.platform !== 'all' || AppState.platformCategory !== 'all' || AppState.activePlaylist)) {
        if (AppState.activePlaylist) renderCollectionDetails(AppState.activePlaylist, filteredGames(), 'Playlist');
        else if (AppState.platformCategory !== 'all') renderCollectionDetails(AppState.platformCategory, AppState.games.filter(item => platformCategoryFor(item) === AppState.platformCategory), 'Category');
        else renderPlatformDetails(AppState.platform);
        return;
      }
      if (!game) { $('details').innerHTML = '<div class="detail-empty">Select a game to inspect its real metadata and artwork.</div>'; return; }
      const heroStyle = game.has_background ? `style="background-image:url('${media(game,'background')}')"` : '';
      $('details').innerHTML = `<div class="hero motion-enter" ${heroStyle}><div class="hero-copy"><div class="hero-kicker">${escapeHtml(game.platform || 'Unspecified platform')}</div><h2>${escapeHtml(game.name)}</h2></div></div>
        <div class="detail-body">
          <div class="rating"><strong>${game.favorite ? '★ Favorite' : game.rating ? `${game.rating} ★` : 'Library'}</strong><span>${escapeHtml(game.progress || game.genre || '')}</span><span class="badge-row">${renderBadges(game)}</span></div>
          <button class="play" id="playButton" ${game.path_exists && game.store_installed !== false ? '' : game.gameyfin_id && !game.store_installed ? '' : 'disabled'}>${game.gameyfin_id && !game.store_installed ? '⬇ INSTALL' : '▶ PLAY'}</button>
          <div class="detail-actions"><button class="icon-button" id="favoriteButton">${game.favorite ? 'Remove favorite' : 'Add favorite'}</button><button class="icon-button" id="editButton">Edit metadata</button><button class="icon-button" id="databaseMetadataButton">Find metadata</button>${game.steam_app_id ? '<button class="icon-button" id="steamMetadataButton">Use Steam data</button>' : ''}<button class="icon-button" id="captureScreenshot">Capture screenshot</button><button class="icon-button" id="downloadBezel">Download bezel</button>${game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="uninstallGameyfin">Uninstall Gameyfin copy</button>' : ''}<button class="icon-button" id="removeGameButton">Remove game</button></div>
          <div class="detail-card"><h3>Information</h3><div class="facts">
            ${fact('Release date',game.year)}${fact('Developer',game.developer)}${fact('Publisher',game.publisher)}${fact('ESRB',game.esrb)}${fact('Source',game.source)}${fact('Category',platformCategoryFor(game))}${Object.entries(game.custom_fields || {}).map(([key,value]) => fact(key,value)).join('')}${fact('Max players',game.max_players)}${fact('Controller support',game.controller_support)}${fact('Disc count',game.disc_count)}${fact('Play time',duration(game.playtime_seconds))}
            ${fact('Launches',game.play_count)}${fact('Last played',game.last_played ? game.last_played.replace('T',' ') : '')}${fact('Progress',game.progress)}${fact('Rating',game.rating ? `${game.rating} / 5` : '')}${fact('Region',game.region)}${fact('Play mode',game.play_mode)}${fact('Wikipedia',game.wikipedia_url)}${fact('Video URL',game.video_url)}
          </div>${game.description ? `<p class="description">${escapeHtml(game.description)}</p>` : ''}${game.notes ? `<p class="description"><strong>Notes</strong><br>${escapeHtml(game.notes)}</p>` : ''}
          ${game.applications.length || game.versions.length || game.documents.length ? `<div class="extras">${game.applications.map((item,index) => `<button class="icon-button" data-extra="applications:${index}">App · ${escapeHtml(item.name)}</button>`).join('')}${game.versions.map((item,index) => `<button class="icon-button" data-extra="versions:${index}">Version · ${escapeHtml(item.name)}</button>`).join('')}${game.documents.map((item,index) => `<button class="icon-button" data-document="${index}">Read ${escapeHtml(item.name)}</button><button class="icon-button" data-extra="documents:${index}" aria-label="Open ${escapeHtml(item.name)} externally">↗</button>`).join('')}</div>` : ''}</div>
          ${game.has_video ? `<div class="detail-card"><h3>Video</h3><video class="media-player" controls preload="metadata" src="${media(game,'video')}"></video></div>` : ''}
          ${game.has_music ? `<div class="detail-card"><h3>Music</h3><audio class="media-player" controls preload="metadata" src="${media(game,'music')}"></audio></div>` : ''}
          ${game.available_screenshots.length ? `<div class="detail-card"><h3>Screenshots</h3><div class="screenshot-grid">${game.available_screenshots.map(index => `<button data-screenshot="${index}" aria-label="Open screenshot ${index + 1}"><img src="${media(game,'screenshot',index)}" alt="" loading="lazy" decoding="async"></button>`).join('')}</div></div>` : ''}
          ${renderArtwork(game)}
          <div class="detail-card"><h3>Related Games</h3><div class="related-grid" id="relatedGames"><span class="description">Finding matches...</span></div></div>
          ${AppState.raConfigured ? `<div class="detail-card"><h3>RetroAchievements</h3><div id="achievementContent"><p class="description">${game.ra_game_id ? `Matched to game ${escapeHtml(game.ra_game_id)}.` : 'Match this ROM to load achievements.'}</p><div class="extras">${game.steam_app_id ? '<button class="icon-button" id="downloadTrailer">Download Steam trailer</button>' : ''}${game.heroic_app_id ? '<button class="icon-button" id="downloadGogMedia">Download GOG media</button>' : ''}<button class="icon-button" id="openBrowser">Open Wikipedia</button></div><button class="icon-button" id="loadAchievements">Load achievements</button><div id="achievementStats"></div></div></div>` : ''}
          <div class="detail-card"><h3>Save management</h3><div class="extras"><button class="icon-button" id="discoverSaves">Discover locations</button>${game.save_paths.length ? '<button class="icon-button" id="backupSaves">Back up now</button>' : ''}<button class="icon-button" id="ludusaviBackup">Ludusavi backup</button><button class="icon-button" id="ludusaviRestore">Ludusavi restore</button>${AppState.appSettings.save_tools?.hoard ? '<button class="icon-button" id="hoardBackup">Hoard backup</button>' : ''}${game.platform === 'Arcade' || game.rom_name ? '<button class="icon-button" id="exportHighscores">Export high scores</button>' : ''}</div><div class="description" id="saveDiscovery">${game.save_paths.length ? `${game.save_paths.length} configured location${game.save_paths.length === 1 ? '' : 's'}` : 'No save location configured.'}${AppState.appSettings.save_tools?.ludusavi ? ' · Ludusavi detected' : ' · Install ludusavi for automatic save discovery'}</div><div class="extras" id="saveBackups"></div></div>
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
        $('mediaDialog').append(image);
        $('mediaDialog').showModal();
      });
      document.querySelectorAll('[data-artwork]').forEach(button => button.onclick = () => {
        const image = document.createElement('img');
        image.id = 'fullScreenshot';
        image.alt = button.getAttribute('aria-label') || 'Game artwork';
        image.decoding = 'async';
        image.src = media(game,button.dataset.artwork);
        $('mediaDialog').append(image);
        $('mediaDialog').showModal();
      });
      document.querySelectorAll('[data-manual]').forEach(button => button.onclick = () => {
        window.open(button.dataset.manual, '_blank');
      });
      document.querySelectorAll('[data-document]').forEach(button => button.onclick = () => openReader(game, Number(button.dataset.document)));
      if ($('loadAchievements')) $('loadAchievements').onclick = () => loadAchievements(game.id);
      if ($('downloadTrailer')) $('downloadTrailer').onclick = async () => { try { await api('/api/metadata/trailer',{method:'POST',body:JSON.stringify({id:game.id})}); await refresh(); renderDetails(); notify('Steam trailer downloaded'); } catch(error) { notify(error.message); } };
      if ($('downloadGogMedia')) $('downloadGogMedia').onclick = async () => { try { await api('/api/metadata/gog',{method:'POST',body:JSON.stringify({id:game.id})}); await refresh(); renderDetails(); notify('GOG media downloaded'); } catch(error) { notify(error.message); } };
      if ($('openBrowser')) $('openBrowser').onclick = () => { const url = game.wikipedia_url || `https://en.wikipedia.org/wiki/Special:Search?search=${encodeURIComponent(game.name)}`; window.open(url, '_blank'); };
      if ($('backupSaves')) {
        $('backupSaves').onclick = () => backupSaves(game.id);
        loadBackups(game.id);
      }
      if ($('ludusaviBackup')) $('ludusaviBackup').onclick = () => ludusaviAction(game.id, 'backup');
      if ($('ludusaviRestore')) $('ludusaviRestore').onclick = () => ludusaviAction(game.id, 'restore');
      if ($('hoardBackup')) $('hoardBackup').onclick = () => hoardAction(game.id, 'backup');
      $('discoverSaves').onclick = () => discoverSaves(game.id);
      loadRelated(game.id);
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
    const fact = (label,value) => `<div class="fact"><small>${label}</small><span>${escapeHtml(value || '-')}</span></div>`;
    function render() { renderPlatformCategories(); renderPlatforms(); renderPlaylists(); renderFilterPresets(); renderGrid(); renderDetails(); applySidebarVisibility(); $('status').textContent = `${AppState.games.length} games · local library`; }
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
        const value = prompt(`Platform documents for ${platformName}, one per line: Name | Path`, current);
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
    function openReader(game, index, customUrl) {
      const doc = game.documents[index];
      if (!doc) return;
      AppState.readerPage = 1;
      AppState.readerUrl = customUrl || `/api/document?id=${game.id}&index=${index}&token=${encodeURIComponent(token)}`;
      $('readerTitle').textContent = doc.name;
      $('readerViewport').classList.remove('spread');
      $('readerFrame').style.filter = '';
      setReaderPage(1);
      $('readerDialog').showModal();
    }
    function setReaderPage(page) {
      AppState.readerPage = Math.max(1, page);
      const suffix = AppState.readerUrl.toLowerCase().includes('.pdf') || AppState.readerUrl.includes('/api/document') ? `#page=${AppState.readerPage}` : '';
      $('readerFrame').src = `${AppState.readerUrl}${suffix}`;
      $('readerPageLabel').textContent = `Page ${AppState.readerPage}`;
    }
    async function launch(id, trigger = $('playButton')) {
      setButtonBusy(trigger, true);
      try {
        const result = await api('/api/launch',{method:'POST',body:JSON.stringify({id})});
        showLifecycle('Starting', result.game, 'The game process is running', 1800);
        await refresh();
      } catch(error) { notify(error.message); }
      finally { setButtonBusy(trigger, false); }
    }
    function showLifecycle(kind, game, message, milliseconds) {
      $('lifecycleKind').textContent = kind;
      $('lifecycleGame').textContent = game;
      $('lifecycleMessage').textContent = message;
      $('lifecycle').hidden = false;
      clearTimeout(showLifecycle.timer);
      showLifecycle.timer = setTimeout(() => $('lifecycle').hidden = true, milliseconds);
    }
    let sessionPollBusy = false;
    let sessionPollIdle = false;
    function scheduleSessionPoll(delay) { setTimeout(pollSessions, delay); }
    async function pollSessions() {
      if (sessionPollBusy) {
        setTimeout(pollSessions, 1000);
        return;
      }
      sessionPollBusy = true;
      try {
        const result = await api(`/api/running?after=${AppState.lastSessionEvent}`);
        AppState.lastSessionEvent = result.last_event;
        AppState.runningGames = result.running;
        const stopped = result.events.filter(event => event.kind === 'stopped').at(-1);
        if (stopped) {
          const exitCode = Number(stopped.exit_code ?? '');
          const shortSession = Number(stopped.seconds ?? 0) < 5;
          const failed = Number.isFinite(exitCode) && exitCode !== 0;
          if (failed && shortSession) {
            showLifecycle('Session failed', stopped.game, `Exited immediately with code ${exitCode}. Check the Launch command and emulator install.`, 5000);
          } else if (failed) {
            showLifecycle('Session ended', stopped.game, `Exited with code ${exitCode}.`, 2500);
          } else {
            showLifecycle('Session ended', stopped.game, 'Play time and history were saved', 1600);
          }
          await refresh();
        }
        $('sessionsButton').textContent = result.running.length ? `Running (${result.running.length})` : 'Running';
        $('sessionsButton').disabled = !result.running.length;
        if (result.running.length) $('status').textContent = `${result.running.length} game${result.running.length === 1 ? '' : 's'} running`;
        if ($('sessionsDialog').open) renderSessions();
      } catch(error) { notify(error.message); }
      finally {
        sessionPollBusy = false;
        // Poll every second while a session is active, every ten when idle.
        sessionPollIdle = !AppState.runningGames.length;
        setTimeout(pollSessions, sessionPollIdle ? 10000 : 1000);
      }
    }
    async function openHistory() {
      try {
        const result = await api('/api/history');
        $('historyList').innerHTML = result.enabled
          ? (result.history.length ? result.history.map(session => `<div class="history-item"><strong>${escapeHtml(session.game)}</strong><br>${escapeHtml(session.started.replace('T',' '))} · ${duration(session.seconds)} · exit ${session.exit_code}</div>`).join('') : '<p class="description">No sessions recorded yet.</p>')
          : '<p class="description">Session history tracking is disabled in Settings.</p>';
        if (!$('historyDialog').open) $('historyDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    function filterSettings() {
      const query = $('settingsSearch').value.toLowerCase().trim();
      document.querySelectorAll('.settings-field').forEach(field => {
        const haystack = `${field.dataset.setting || ''} ${field.textContent || ''}`.toLowerCase();
        field.hidden = query && !haystack.includes(query);
      });
    }
    async function completeWelcome() {
      try {
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify({...collectSettings(),welcome_completed:true})});
        $('welcomeDialog').close();
      } catch(error) { notify(error.message); }
    }
    function maybeShowWelcome() {
      if (!AppState.appSettings.welcome_completed && !AppState.games.length) $('welcomeDialog').showModal();
    }
    function collectSettings() {
      return {
        watch_folders:$('watchFolders').value.split('\n').map(value => value.trim()).filter(Boolean),
        cloud_folder:$('cloudFolder').value.trim(),
        screensaver_seconds:Number($('screensaverSeconds').value),
        startup_commands:$('startupCommands').value.split('\n').map(value => value.trim()).filter(Boolean),
        shutdown_commands:$('shutdownCommands').value.split('\n').map(value => value.trim()).filter(Boolean),
        track_session_history:$('trackSessionHistory').checked,
        backup_on_close:$('backupOnClose').checked,
        save_backup_limit:Number($('saveBackupLimit').value),
        media_download_limit:Number($('mediaDownloadLimit').value),
        auto_import_media_types:$('autoImportMediaTypes').value.split(',').map(value => value.trim()).filter(Boolean),
        region_priority:$('regionPriority').value.split(',').map(value => value.trim()).filter(Boolean),
        library_music:$('libraryMusic').value.trim(),
        video_bgm_mix:$('videoBgmMix').checked,
        bigbox_mode:$('bigBoxMode').value,
        show_playlist_actions:$('showPlaylistActions').checked,
        dynamic_play_button:$('dynamicPlayButton').checked,
        hidden_sidebar_sections:$('hiddenSidebarSections').value.split(',').map(value => value.trim()).filter(Boolean),
        obs_auto_attach:$('obsAutoAttach').checked,
        obs_recording_path:$('obsRecordingPath').value.trim(),
        progress_automation_enabled:$('progressAutomationEnabled').checked,
        progress_automation_play_minutes:Number($('progressAutomationMinutes').value),
        progress_automation_idle_days:Number($('progressAutomationIdleDays').value),
        progress_on_first_play:$('progressOnFirstPlay').value,
        tracking_mode:$('trackingMode').value,
        tracking_delay:Number($('trackingDelay').value),
        tracking_frequency:Number($('trackingFrequency').value),
        apply_perf:$('applyPerf').value,
        auto_close_store_clients:$('autoCloseStoreClients').checked,
        image_group:$('defaultImageGroup').value,
        badge_visibility:[...document.querySelectorAll('[data-badge-setting]:checked')].map(input => input.dataset.badgeSetting),
        controller_map:Object.fromEntries([...document.querySelectorAll('[data-controller]')].map(input => [input.dataset.controller,Number(input.value)])),
        welcome_completed:AppState.appSettings.welcome_completed,
        locale:$('localeSetting').value,
        library_view:$('libraryViewSetting').value,
        custom_field_defs:($('customFieldDefs').value || '').split('\n').map(line => line.trim()).filter(Boolean).map(line => { const [name,...rest] = line.split('|'); return {name:(name || '').trim(), options:rest.join('|').split(',').map(value => value.trim()).filter(Boolean)}; }).filter(item => item.name),
        bigbox_startup_video:$('bigboxStartupVideo').value.trim(),
        bigbox_shutdown_commands:$('bigboxShutdownCommands').value.split('\n').map(value => value.trim()).filter(Boolean),
        attract_mode_seconds:Number($('attractModeSeconds').value || $('screensaverSeconds').value || 90),
        tray_enabled:$('trayEnabled').checked,
        minimize_to_tray:$('minimizeToTray').checked,
        ui_window:$('uiWindow').value || 'app',
        ludusavi_backup_path:$('ludusaviBackupPath')?.value.trim() || '',
      };
    }
    function collectStorefrontSettings() {
      return {
        storefront_auto_import:{
          steam:$('storefrontAutoImportSteam').checked,
          heroic:$('storefrontAutoImportHeroic').checked,
          lutris:$('storefrontAutoImportLutris').checked,
          gameyfin:Boolean($('storefrontAutoImportGameyfin')?.checked),
        },
        gameyfin_url:$('storefrontGameyfinUrl')?.value.trim() || '',
        gameyfin_username:$('storefrontGameyfinUsername')?.value.trim() || '',
        gameyfin_password:$('storefrontGameyfinPassword')?.value || '',
        gameyfin_install_dir:$('storefrontGameyfinInstallDir')?.value.trim() || '',
      };
    }
    async function saveEmumoviesSettings() {
      const username = $('emumoviesUsername').value.trim();
      const password = $('emumoviesPassword').value;
      if (!username || !password) return;
      await api('/api/emumovies/settings',{method:'POST',body:JSON.stringify({username,password})});
    }
    let shuttingDown = false;
    async function gracefulShutdown(force = false) {
      if (shuttingDown) return;
      shuttingDown = true;
      $('shutdownOverlay').hidden = false;
      $('shutdownMessage').textContent = force ? 'Force closing running games...' : 'Closing running games before exit...';
      try { await api('/api/shutdown',{method:'POST',body:JSON.stringify({force})}); } catch(error) {}
      $('shutdownMessage').textContent = 'Running shutdown applications...';
      window.close();
    }
    async function favorite(id) { try { const result = await api('/api/favorite',{method:'POST',body:JSON.stringify({id})}); const game = AppState.games.find(item => item.id === id); if (game) game.favorite = result.favorite; renderGrid(); renderDetails(); } catch(error) { notify(error.message); } }
    function closeContextMenu() { $('contextMenu').hidden = true; AppState.contextGameId = null; }
    function openContextMenu(event, id) {
      event.preventDefault();
      AppState.contextGameId = id;
      AppState.selectedId = id;
      const menu = $('contextMenu');
      $('contextPlaylist').innerHTML = '<option value="">Add to playlist...</option>' + AppState.playlists.filter(item => item.type === 'manual').map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('');
      menu.hidden = false;
      menu.style.left = `${Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 8)}px`;
      menu.style.top = `${Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 8)}px`;
    }
    async function updateGameStatus(id, progress) {
      const game = AppState.games.find(item => item.id === id);
      if (!game) return;
      try { await api('/api/game',{method:'POST',body:JSON.stringify({id,game:{...game,progress}})}); await refresh(); notify(`Progress set to ${progress || 'Not set'}`); } catch(error) { notify(error.message); }
    }
    async function removeGame(id,name) {
      if (!confirm(`Remove "${name}" from OpenBox? Game files will not be deleted.`)) return;
      const alsoDeleteMedia = confirm('Also delete associated media files listed in this game entry?');
      try { await api('/api/game/delete',{method:'POST',body:JSON.stringify({id,delete_media:alsoDeleteMedia})}); AppState.selectedId = null; await refresh(); notify('Game removed from library'); } catch(error) { notify(error.message); }
    }
    async function launchExtra(id,kind,index) { try { await api('/api/extra/launch',{method:'POST',body:JSON.stringify({id,kind,index})}); notify('Opened'); } catch(error) { notify(error.message); } }
    async function openSessions() {
      try {
        const result = await api(`/api/running?after=${AppState.lastSessionEvent}`);
        AppState.runningGames = result.running;
        AppState.lastSessionEvent = result.last_event;
        renderSessions();
        if (!$('sessionsDialog').open) $('sessionsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    function renderSessions() {
      $('sessionList').innerHTML = AppState.runningGames.length ? AppState.runningGames.map(session => {
        const game = AppState.games.find(item => item.id === session.game_id);
        const extras = game ? `${game.documents.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:documents:${index}">Read ${escapeHtml(item.name)}</button>`).join('')}${game.applications.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:applications:${index}">${escapeHtml(item.name)}</button>`).join('')}${game.versions.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:versions:${index}">Version · ${escapeHtml(item.name)}</button>`).join('')}${game.save_paths.length ? `<button class="icon-button" data-session-backup="${game.id}">Back up saves</button>` : ''}` : '';
        return `<div class="detail-card"><h3>${escapeHtml(session.game)}</h3><p class="description">${session.paused ? 'Paused' : 'Running'} · PID ${session.pid} · started ${escapeHtml(session.started.replace('T',' '))}</p><div class="extras"><button class="primary" data-session-action="${session.launch_id}:${session.paused ? 'resume' : 'pause'}">${session.paused ? 'Resume' : 'Pause'}</button><button class="icon-button" data-session-action="${session.launch_id}:restart">Restart</button><button class="icon-button" data-session-action="${session.launch_id}:stop">Exit</button><button class="icon-button" data-session-action="${session.launch_id}:kill">Force close</button>${extras}</div></div>`;
      }).join('') : '<p class="description">No games are running.</p>';
      document.querySelectorAll('[data-session-action]').forEach(button => button.onclick = async () => {
        const [launch_id,action] = button.dataset.sessionAction.split(':');
        if (action === 'kill' && !confirm('Force close this game? Unsaved progress may be lost.')) return;
        try {
          await api('/api/session/control',{method:'POST',body:JSON.stringify({launch_id,action})});
          notify(action === 'pause' ? 'Game paused' : action === 'resume' ? 'Game resumed' : action === 'restart' ? 'Restarting game' : 'Closing game');
          setTimeout(openSessions, 180);
        } catch(error) { notify(error.message); }
      });
      document.querySelectorAll('[data-session-extra]').forEach(button => button.onclick = () => {
        const [id,kind,index] = button.dataset.sessionExtra.split(':');
        launchExtra(Number(id),kind,Number(index));
      });
      document.querySelectorAll('[data-session-backup]').forEach(button => button.onclick = () => backupSaves(Number(button.dataset.sessionBackup)));
    }
    async function loadBackups(id) {
      try {
        const result = await api(`/api/saves?id=${id}`);
        if (!$('saveBackups')) return;
        $('saveBackups').innerHTML = result.backups.slice(0,8).map(backup => `<button class="icon-button" data-backup="${escapeHtml(backup.name)}">${escapeHtml(backup.name)}</button>`).join('');
        document.querySelectorAll('[data-backup]').forEach(button => button.onclick = () => restoreSaves(id,button.dataset.backup));
      } catch(error) { notify(error.message); }
    }
    async function backupSaves(id) { try { const result = await api('/api/saves/backup',{method:'POST',body:JSON.stringify({id})}); notify(`Created ${result.backup}`); loadBackups(id); } catch(error) { notify(error.message); } }
    async function restoreSaves(id,backup) {
      if (!confirm(`Restore ${backup}? Current saves will be backed up first.`)) return;
      try { await api('/api/saves/restore',{method:'POST',body:JSON.stringify({id,backup})}); notify('Save restored'); loadBackups(id); } catch(error) { notify(error.message); }
    }
    async function discoverSaves(id) {
      try {
        const result = await api(`/api/saves/discover?id=${id}`);
        $('saveDiscovery').innerHTML = result.candidates.length ? result.candidates.map(item => `<button class="icon-button" data-save-path="${escapeHtml(item.path)}">${item.shared ? 'Add shared location' : 'Add'} · ${escapeHtml(item.label)}<br><small>${escapeHtml(item.path)}</small></button>`).join('') : 'No new save locations were detected.';
        document.querySelectorAll('[data-save-path]').forEach(button => button.onclick = async () => {
          try { await api('/api/saves/add',{method:'POST',body:JSON.stringify({id,path:button.dataset.savePath})}); await refresh(); notify('Save location added'); } catch(error) { notify(error.message); }
        });
      } catch(error) { notify(error.message); }
    }
    async function steamMetadata(id) { try { notify('Downloading Steam metadata and artwork'); await api('/api/metadata/steam',{method:'POST',body:JSON.stringify({id})}); await refresh(); notify('Steam metadata updated'); } catch(error) { notify(error.message); } }
    async function openMetadata(game) {
      AppState.metadataGameId = game.id;
      $('metadataQuery').value = game.name;
      $('metadataResults').innerHTML = '';
      if (!$('metadataDialog').open) $('metadataDialog').showModal();
      try {
        const status = await api('/api/metadata/status');
        renderMetadataStatus(status);
        if (status.ready) searchMetadata();
      } catch(error) { notify(error.message); }
    }
    function renderMetadataStatus(status) {
      const state = status.job.state;
      $('metadataStatus').textContent = status.ready ? 'Local database ready.' : state === 'downloading' ? 'Downloading and indexing the official database...' : state === 'error' ? status.job.error : 'Download the official metadata database before searching.';
      $('syncMetadata').disabled = state === 'downloading';
      const coverage = status.coverage;
      const coverageBox = $('metadataCoverage');
      const factsBox = $('metadataCoverageFacts');
      if (coverageBox) {
        if (status.ready && coverage && coverage.games) {
          const fields = [
            ['with_cover', 'Games with box front'],
            ['with_background', 'Games with background'],
            ['with_box_back', 'Games with box back'],
            ['with_cart_front', 'Games with cart front'],
            ['with_disc', 'Games with disc'],
            ['with_advertisement', 'Games with ads / flyers'],
            ['with_title_screen', 'Games with title screen'],
            ['with_clear_logo', 'Games with clear logo'],
            ['with_manual', 'Games with manual'],
          ];
          factsBox.innerHTML = `${fact('Games', coverage.games)}${fact('Database matched', coverage.matched_games)}${fact('Match ratio', coverage.matched_ratio == null ? '-' : `${Math.round(coverage.matched_ratio * 100)}%`)}${fields.filter(([key]) => coverage[key] != null).map(([key, label]) => fact(label, coverage[key])).join('')}`;
          coverageBox.style.display = '';
        } else {
          coverageBox.style.display = 'none';
        }
      }
    }    async function searchMetadata() {
      try {
        const result = await api(`/api/metadata/search?id=${AppState.metadataGameId}&q=${encodeURIComponent($('metadataQuery').value)}`);
        $('metadataResults').innerHTML = result.results.length ? result.results.map(item => `<div class="metadata-result"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.platform)}${item.release_date ? ` · ${escapeHtml(item.release_date)}` : ''}${item.developer ? ` · ${escapeHtml(item.developer)}` : ''}</small></div><button type="button" class="primary" data-apply-metadata="${item.database_id}">Use</button></div>`).join('') : '<p class="description">No matching games found.</p>';
        document.querySelectorAll('[data-apply-metadata]').forEach(button => button.onclick = () => applyMetadata(button.dataset.applyMetadata));
      } catch(error) { notify(error.message); }
    }
    async function applyMetadata(databaseId) {
      const media = [['cover','metadataCover'],['background','metadataBackground'],['screenshots','metadataScreenshots'],['box_back','metadataBoxBack'],['box_spine','metadataBoxSpine'],['box_3d','metadataBox3d'],['clear_logo','metadataClearLogo'],['fanart','metadataFanart'],['banner','metadataBanner'],['icon','metadataIcon'],['title_screen','metadataTitleScreen'],['cart_front','metadataCartFront'],['cart_back','metadataCartBack'],['disc','metadataDisc'],['advertisement','metadataAdvertisement'],['manual','metadataManual']].filter(([,id]) => $(id).checked).map(([name]) => name);
      try {
        notify('Downloading selected metadata and media');
        const result = await api('/api/metadata/apply',{method:'POST',body:JSON.stringify({id:AppState.metadataGameId,database_id:databaseId,media,overwrite:$('metadataOverwrite').checked})});
        $('metadataDialog').close();
        await refresh();
        notify((result.notes || []).length ? result.notes.join(' · ') : 'Metadata applied');
      } catch(error) { notify(error.message); }
    }
    $('metadataSearchForm').onsubmit = event => { event.preventDefault(); searchMetadata(); };
    $('syncMetadata').onclick = async () => {
      try {
        await api('/api/metadata/sync',{method:'POST',body:'{}'});
        renderMetadataStatus({ready:false,job:{state:'downloading'}});
        watchMetadata();
      } catch(error) { notify(error.message); }
    };
    $('searchIgdb').onclick = async () => {
      const game = games.find(item => item.id === metadataGameId);
      try {
        const result = await api(`/api/metadata/igdb/search?q=${encodeURIComponent($('metadataQuery').value)}&AppState.platform =${encodeURIComponent(game?.platform || '')}`);
        $('metadataResults').innerHTML = result.results.length ? result.results.map(item => `<div class="metadata-result"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.platforms || '')}${item.year ? ` · ${escapeHtml(item.year)}` : ''}</small><p class="description">${escapeHtml(item.summary || '')}</p></div><button type="button" class="primary" data-apply-igdb="${item.id}">Use</button></div>`).join('') : '<p class="description">No IGDB matches found.</p>';
        document.querySelectorAll('[data-apply-igdb]').forEach(button => button.onclick = async () => {
          try {
            await api('/api/metadata/igdb/apply',{method:'POST',body:JSON.stringify({id:metadataGameId,igdb_id:Number(button.dataset.applyIgdb)})});
            $('metadataDialog').close();
            await refresh();
            notify('IGDB metadata applied');
          } catch(error) { notify(error.message); }
        });
      } catch(error) { notify(error.message); }
    };
    async function watchMetadata() {
      try {
        const status = await api('/api/metadata/status');
        renderMetadataStatus(status);
        if (status.job.state === 'downloading') return setTimeout(watchMetadata, 1500);
        if (status.ready) { notify('Metadata database ready'); searchMetadata(); }
      } catch(error) { notify(error.message); }
    }
    async function loadAchievements(id) {
      try {
        $('achievementContent').innerHTML = '<p class="description">Matching ROM and loading progress...</p>';
        const result = await api('/api/ra/game',{method:'POST',body:JSON.stringify({id})});
        await refresh();
        if ($('achievementContent')) {
          $('achievementContent').innerHTML = `<p class="description">${result.earned} of ${result.total} earned · ${escapeHtml(result.completion)}${result.earned_hardcore ? ` · ${result.earned_hardcore} hardcore` : ''}${result.beaten ? ` · beaten ${result.beaten}` : ''}${result.mastered ? ` · mastered ${result.mastered}` : ''}${result.motivation ? ` · ${escapeHtml(result.motivation)}` : ''}</p>${(result.achievements || []).map(item => `<div class="achievement"><img src="/api/ra/badge?name=${encodeURIComponent(item.badge)}&locked=${item.earned ? 0 : 1}&token=${encodeURIComponent(token)}" alt="" loading="lazy" decoding="async"><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.description)}</small></div><span>${item.points} pts${item.hardcore ? ' ★' : ''}</span></div>`).join('')}`;
        }
      } catch(error) { notify(error.message); renderDetails(); }
    }
    async function openGameDialog(game = null) {
      await ensureProfiles();
      AppState.editingId = game ? game.id : null;
      $('dialogTitle').textContent = game ? 'Edit game' : 'Add game';
      [...$('gameForm').elements].forEach(element => {
        if (!element.name) return;
        if (element.type === 'checkbox') element.checked = Boolean(game?.[element.name]);
        else if (element.name === 'applications') element.value = (game?.applications || []).map(item => [item.name,item.path,item.command].filter(Boolean).join(' | ')).join('\n');
        else if (element.name === 'versions') element.value = (game?.versions || []).map(item => [item.name,item.path,item.command].filter(Boolean).join(' | ')).join('\n');
        else if (element.name === 'documents') element.value = (game?.documents || []).map(item => [item.name,item.path].join(' | ')).join('\n');
        else if (element.name === 'save_paths') element.value = (game?.save_paths || []).join('\n');
        else if (element.name === 'screenshots') element.value = (game?.screenshots || []).join('\n');
        else if (element.name === 'alternate_names') element.value = Array.isArray(game?.alternate_names) ? game.alternate_names.join('; ') : (game?.alternate_names || '');
        else element.value = game?.[element.name] || '';
      });
      const container = $('customFieldInputs');
      const defs = AppState.appSettings.custom_field_defs || [];
      container.innerHTML = defs.length ? defs.map(def => {
        const value = game?.custom_fields?.[def.name] || '';
        if (def.options?.length) {
          return `<label class="field"><span>${escapeHtml(def.name)}</span><select data-custom-field="${escapeHtml(def.name)}">${['', ...def.options].map(option => `<option value="${escapeHtml(option)}" ${option === value ? 'selected' : ''}>${escapeHtml(option || 'Not set')}</option>`).join('')}</select></label>`;
        }
        return `<label class="field"><span>${escapeHtml(def.name)}</span><input data-custom-field="${escapeHtml(def.name)}" value="${escapeHtml(value)}"></label>`;
      }).join('') : '';
      const profileSelect = $('gameForm').elements.launch_profile;
      const platformName = $('gameForm').elements.platform.value.trim();
      if (profileSelect) {
        const currentProfile = game?.launch_profile || '';
        profileSelect.innerHTML = '<option value="">Default platform profile</option>' + Object.keys(AppState.availableProfiles).filter(name => !platformName || name === platformName).map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
        profileSelect.value = currentProfile;
      }
      openDialog($('gameDialog'));
    }
    $('gameForm').onsubmit = async event => {
      event.preventDefault();
      const game = Object.fromEntries(new FormData(event.currentTarget));
      game.extract_archive = event.currentTarget.elements.extract_archive.checked;
      game.hidden = event.currentTarget.elements.hidden.checked;
      game.hide_in_bigbox = event.currentTarget.elements.hide_in_bigbox.checked;
      game.alternate_names = game.alternate_names.split(';').map(value => value.trim()).filter(Boolean);
      game.applications = game.applications.split('\n').filter(Boolean).map(line => { const [name,path,command=''] = line.split('|').map(value => value.trim()); return {name,path,command}; });
      game.versions = game.versions.split('\n').filter(Boolean).map(line => { const [name,path,command=''] = line.split('|').map(value => value.trim()); return {name,path,command}; });
      game.documents = game.documents.split('\n').filter(Boolean).map(line => { const [name,path] = line.split('|').map(value => value.trim()); return {name,path}; });
      game.save_paths = game.save_paths.split('\n').map(value => value.trim()).filter(Boolean);
      game.screenshots = game.screenshots.split('\n').map(value => value.trim()).filter(Boolean);
      game.custom_fields = Object.fromEntries([...document.querySelectorAll('[data-custom-field]')].map(input => [input.dataset.customField, input.value.trim()]).filter(([, value]) => value));
      try { await api('/api/game',{method:'POST',body:JSON.stringify({id:editingId,game})}); $('gameDialog').close(); await refresh(); notify('Library saved'); } catch(error) { notify(error.message); }
    };
    async function importFolder() {
      const folder = prompt('Enter the absolute path of the folder to import.');
      if (!folder) return;
      try {
        const preview = await api('/api/import',{method:'POST',body:JSON.stringify({folder,recommend:true})});
        const chosen = {};
        for (const [platform, items] of Object.entries(preview.recommendations || {})) {
          if (!items?.length) continue;
          if (items.length === 1) { chosen[platform] = items[0].app_id; continue; }
          const labels = items.map((item,index) => `${index + 1}. ${item.name}`).join('\n');
          const pick = prompt(`Multiple emulators for ${platform}:\n${labels}\nEnter number to install, or leave blank to skip`, '1');
          const index = Number(pick) - 1;
          if (pick && items[index]) chosen[platform] = items[index].app_id;
        }
        const result = Object.keys(chosen).length
          ? await api('/api/import/wizard',{method:'POST',body:JSON.stringify({folder,chosen_emulators:chosen})})
          : preview;
        await refresh();
        notify(`${result.added} games imported${result.installed?.length ? ` · installed ${result.installed.length} emulator(s)` : ''}`);
      } catch(error) { notify(error.message); }
    }
    async function importSteam() {
      try {
        const result = await api('/api/import/steam',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} Steam games imported · ${result.found} installed`);
      } catch(error) { notify(error.message); }
    }
    async function importHeroic() {
      try {
        const result = await api('/api/import/heroic',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} Heroic games imported · ${result.found} installed`);
      } catch(error) { notify(error.message); }
    }
    async function importLutris() {
      try {
        const result = await api('/api/import/lutris',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} Lutris games imported · ${result.found} installed`);
      } catch(error) { notify(error.message); }
    }
    async function importArcade() {
      const folder = prompt('Absolute path of the arcade ROM folder');
      if (!folder) return;
      const source = prompt('Set type: MAME or FinalBurn Neo', 'MAME');
      if (!source) return;
      const dat = prompt('Absolute DAT/XML path. Leave blank to use installed MAME metadata.', '') ?? '';
      const command = prompt('Launch command. Leave blank for the detected emulator. You can use {rom_name} and {path}.', '') ?? '';
      try {
        const result = await api('/api/import/arcade',{method:'POST',body:JSON.stringify({folder,source,dat,command})});
        await refresh();
        notify(`${result.added} arcade games imported · ${result.found} matched`);
      } catch(error) { notify(error.message); }
    }
    async function openSettings() {
      try {
        AppState.appSettings = await api('/api/settings');
        $('watchFolders').value = AppState.appSettings.watch_folders.join('\n');
        $('cloudFolder').value = AppState.appSettings.cloud_folder || '';
        $('cloudStatus').textContent = AppState.appSettings.last_cloud_sync ? ` Last synced ${AppState.appSettings.last_cloud_sync.replace('T',' ')}` : '';
        $('screensaverSeconds').value = AppState.appSettings.screensaver_seconds;
        $('startupCommands').value = (AppState.appSettings.startup_commands || []).join('\n');
        $('shutdownCommands').value = (AppState.appSettings.shutdown_commands || []).join('\n');
        $('defaultImageGroup').value = AppState.appSettings.image_group || 'cover';
        const visibleBadges = new Set(AppState.appSettings.badge_visibility || defaultBadges);
        document.querySelectorAll('[data-badge-setting]').forEach(input => input.checked = visibleBadges.has(input.dataset.badgeSetting));
        $('trackSessionHistory').checked = AppState.appSettings.track_session_history !== false;
        $('backupOnClose').checked = Boolean(AppState.appSettings.backup_on_close);
        $('progressAutomationEnabled').checked = Boolean(AppState.appSettings.progress_automation_enabled);
        $('progressAutomationMinutes').value = AppState.appSettings.progress_automation_play_minutes ?? 30;
        $('progressAutomationIdleDays').value = AppState.appSettings.progress_automation_idle_days ?? 30;
        if ($('progressOnFirstPlay')) $('progressOnFirstPlay').value = AppState.appSettings.progress_on_first_play ?? 'Playing';
        if ($('trackingMode')) $('trackingMode').value = AppState.appSettings.tracking_mode || 'default';
        if ($('trackingDelay')) $('trackingDelay').value = AppState.appSettings.tracking_delay ?? 0;
        if ($('trackingFrequency')) $('trackingFrequency').value = AppState.appSettings.tracking_frequency ?? 2;
        if ($('applyPerf')) $('applyPerf').value = AppState.appSettings.apply_perf || 'auto';
        if ($('autoCloseStoreClients')) $('autoCloseStoreClients').checked = Boolean(AppState.appSettings.auto_close_store_clients);
        $('saveBackupLimit').value = AppState.appSettings.save_backup_limit ?? 10;
        $('mediaDownloadLimit').value = AppState.appSettings.media_download_limit ?? 0;
        $('autoImportMediaTypes').value = (AppState.appSettings.auto_import_media_types || ['cover','background','screenshots']).join(', ');
        $('regionPriority').value = (AppState.appSettings.region_priority || ['North America','World','Europe','Japan']).join(', ');
        $('libraryMusic').value = AppState.appSettings.library_music || '';
        $('videoBgmMix').checked = Boolean(AppState.appSettings.video_bgm_mix);
        $('bigBoxMode').value = AppState.appSettings.bigbox_mode || 'stage';
        $('showPlaylistActions').checked = AppState.appSettings.show_playlist_actions !== false;
        $('dynamicPlayButton').checked = AppState.appSettings.dynamic_play_button !== false;
        $('hiddenSidebarSections').value = (AppState.appSettings.hidden_sidebar_sections || []).join(', ');
        $('obsAutoAttach').checked = AppState.appSettings.obs_auto_attach !== false;
        $('obsRecordingPath').value = AppState.appSettings.obs_recording_path || '';
        $('localeSetting').value = AppState.appSettings.locale || 'en';
        $('libraryViewSetting').value = AppState.appSettings.library_view || 'grid';
        $('customFieldDefs').value = (AppState.appSettings.custom_field_defs || []).map(item => `${item.name}|${(item.options || []).join(',')}`).join('\n');
        $('bigboxStartupVideo').value = AppState.appSettings.bigbox_startup_video || '';
        $('bigboxShutdownCommands').value = (AppState.appSettings.bigbox_shutdown_commands || []).join('\n');
        $('attractModeSeconds').value = AppState.appSettings.attract_mode_seconds ?? AppState.appSettings.screensaver_seconds ?? 90;
        $('trayEnabled').checked = Boolean(AppState.appSettings.tray_enabled);
        $('minimizeToTray').checked = Boolean(AppState.appSettings.minimize_to_tray);
        if ($('uiWindow')) $('uiWindow').value = AppState.appSettings.ui_window || 'app';
        if ($('ludusaviBackupPath')) $('ludusaviBackupPath').value = AppState.appSettings.ludusavi_backup_path || '';
        $('mediaPackStatus').textContent = (AppState.appSettings.media_packs || []).filter(item => item.active).map(item => item.name).join(', ');
        $('viewToggleButton').textContent = (AppState.appSettings.library_view || 'grid') === 'list' ? 'Grid view' : 'List view';
        $('settingsSearch').value = '';
        filterSettings();
        const mapping = {...defaultControllerMap,...AppState.appSettings.controller_map};
        document.querySelectorAll('[data-controller]').forEach(input => input.value = mapping[input.dataset.controller]);
        $('updateStatus').textContent = `OpenBox ${AppState.appSettings.version}${AppState.appSettings.appimage ? ' · AppImage' : ' · source checkout'}`;
        $('installUpdate').hidden = true;
        $('installDesktop').disabled = !AppState.appSettings.appimage;
        if (!$('settingsDialog').open) $('settingsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    $('settingsForm').onsubmit = async event => {
      event.preventDefault();
      try {
        await saveEmumoviesSettings().catch(() => {});
        AppState.persist();
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectSettings())});
        $('settingsDialog').close();
        notify('Settings saved');
        applyLibraryMusic();
        applySidebarVisibility();
        renderGrid();
      } catch(error) { notify(error.message); }
    };
    $('scanWatched').onclick = async () => {
      try {
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectSettings())});
        const result = await api('/api/import/watch',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} games imported from watched folders${result.errors.length ? ` · ${result.errors.length} errors` : ''}`);
      } catch(error) { notify(error.message); }
    };
    $('removeSteamGames').onclick = async () => {
      if (!confirm('Remove every imported Steam game from OpenBox? Game files and media will stay on disk.')) return;
      try {
        const result = await api('/api/games/delete-steam',{method:'POST',body:'{}'});
        AppState.selectedId = null;
        await refresh();
        notify(`${result.removed} imported Steam game${result.removed === 1 ? '' : 's'} removed from library`);
      } catch(error) { notify(error.message); }
    };
    $('copyDiagnosticLog').onclick = async () => {
      try {
        const result = await api('/api/log');
        const copy = document.createElement('textarea');
        copy.value = result.log;
        document.body.append(copy);
        copy.select();
        document.execCommand('copy');
        copy.remove();
        notify('Diagnostic log copied. Review it before sharing.');
      } catch(error) { notify(error.message); }
    };
    $('syncCloud').onclick = async () => {
      try {
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectSettings())});
        const result = await api('/api/cloud/sync',{method:'POST',body:'{}'});
        $('cloudStatus').textContent = ` Synced ${result.games} games, merged ${result.merged} remote changes`;
        await refresh();
      } catch(error) { notify(error.message); }
    };
    $('checkUpdate').onclick = async () => {
      try {
        $('updateStatus').textContent = 'Checking the verified GitHub release channel...';
        AppState.pendingUpdate = await api('/api/update');
        $('updateStatus').textContent = pendingUpdate.available ? `OpenBox ${pendingUpdate.latest} is available. ${pendingUpdate.notes || ''}` : `OpenBox ${pendingUpdate.current} is current.`;
        $('installUpdate').hidden = !pendingUpdate.available;
      } catch(error) { $('updateStatus').textContent = error.message; }
    };
    $('installUpdate').onclick = async () => {
      if (!pendingUpdate?.available || !confirm(`Install OpenBox ${pendingUpdate.latest}? The current AppImage will be retained as a backup.`)) return;
      try {
        $('installUpdate').disabled = true;
        const result = await api('/api/update/install',{method:'POST',body:'{}'});
        $('updateStatus').textContent = `OpenBox ${result.installed} installed. Restart OpenBox to use it. Backup: ${result.backup}`;
        $('installUpdate').hidden = true;
      } catch(error) { $('updateStatus').textContent = error.message; }
      finally { $('installUpdate').disabled = false; }
    };
    $('installDesktop').onclick = async () => {
      try {
        const result = await api('/api/desktop/install',{method:'POST',body:'{}'});
        notify(`Desktop shortcut installed at ${result.desktop}`);
      } catch(error) { notify(error.message); }
    };
    async function openProfiles() {
      try {
        const [result,emulators,perf] = await Promise.all([api('/api/profiles'),api('/api/emulators'),api('/api/perf_profiles')]);
        $('profilesText').value = Object.entries(result.profiles).sort().map(([platform,command]) => `${platform} = ${command}`).join('\n');
        const detected = Object.entries(result.detected).filter(([platform]) => !result.profiles[platform]);
        $('detectedProfiles').innerHTML = detected.length ? `<button type="button" class="icon-button" id="useDetected">Add ${detected.length} detected profiles</button>` : '';
        if ($('useDetected')) $('useDetected').onclick = () => {
          const lines = Object.entries(result.detected).map(([platform,command]) => `${platform} = ${command}`).join('\n');
          $('profilesText').value = [$('profilesText').value, lines].filter(Boolean).join('\n');
        };
        perfDraft = Object.fromEntries(Object.entries(perf.perf_profiles || {}).map(([name,value]) => [name,{...value}]));
        const names = Object.keys(result.profiles).sort();
        $('perfProfiles').innerHTML = names.length
          ? names.map(name => {
              const entry = perfDraft[name] || {enabled:false,tdp_w:0,restore_tdp_w:0};
              return `<div class="perf-row wide"><span class="perf-name">${escapeHtml(name)}</span><label class="check"><input type="checkbox" data-perf-enabled="${escapeHtml(name)}" ${entry.enabled ? 'checked' : ''}> Enable TDP</label><label class="field"><span>Limit (W)</span><input type="number" min="0" max="60" step="0.5" data-perf-tdp="${escapeHtml(name)}" value="${entry.tdp_w || ''}"></label><label class="field"><span>Restore (W)</span><input type="number" min="0" max="60" step="0.5" data-perf-restore="${escapeHtml(name)}" value="${entry.restore_tdp_w || ''}"></label></div>`;
            }).join('')
          : '<p class="description wide">No emulator profiles yet. Add one above to set a TDP limit.</p>';
        document.querySelectorAll('[data-perf-enabled]').forEach(input => input.onchange = () => { const entry = perfDraft[input.dataset.perfEnabled] ||= {enabled:false,tdp_w:0,restore_tdp_w:0}; entry.enabled = input.checked; });
        document.querySelectorAll('[data-perf-tdp]').forEach(input => input.oninput = () => { const entry = perfDraft[input.dataset.perfTdp] ||= {enabled:false,tdp_w:0,restore_tdp_w:0}; entry.tdp_w = Number(input.value) || 0; });
        document.querySelectorAll('[data-perf-restore]').forEach(input => input.oninput = () => { const entry = perfDraft[input.dataset.perfRestore] ||= {enabled:false,tdp_w:0,restore_tdp_w:0}; entry.restore_tdp_w = Number(input.value) || 0; });
        renderEmulators(emulators.emulators);
        $('profilesDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    function renderEmulators(emulators) {
      $('emulatorCatalog').innerHTML = `<div class="extras"><button type="button" class="primary" id="installAllEmulators">Install all available emulators</button><button type="button" class="icon-button" id="updateAllEmulators">Update installed emulators</button></div>` + emulators.map(emulator => {
        const state = emulator.job.state || (emulator.installed ? 'installed' : 'available');
        const label = state === 'installing' ? 'Installing...' : emulator.installed ? 'Add profiles' : state === 'error' ? 'Retry' : 'Install';
        const openButton = emulator.installed ? `<button type="button" class="icon-button" data-open-emulator="${escapeHtml(emulator.app_id)}">Open</button>` : '';
        const updateButton = emulator.installed && emulator.mode === 'flatpak' ? `<button type="button" class="icon-button" data-update-emulator="${escapeHtml(emulator.app_id)}">Update</button>` : '';
        return `<div class="emulator-item" title="${escapeHtml(emulator.job.error || '')}"><div><strong>${escapeHtml(emulator.name)}</strong><small>${escapeHtml(emulator.platforms.join(' · '))} · ${escapeHtml(state)}</small></div><div class="emulator-actions">${openButton}${updateButton}<button type="button" class="icon-button" ${state === 'installing' || (!emulator.installed && !emulator.can_install) ? 'disabled' : ''} data-emulator="${escapeHtml(emulator.app_id)}" data-installed="${emulator.installed}">${label}</button></div></div>`;
      }).join('');
      $('installAllEmulators').onclick = async () => {
        try {
          await api('/api/emulators/install-all',{method:'POST',body:'{}'});
          notify('Installing all available emulators');
          watchInstallAll();
        } catch(error) { notify(error.message); }
      };
      $('updateAllEmulators').onclick = async () => {
        try {
          await api('/api/emulators/update-all',{method:'POST',body:'{}'});
          notify('Updating installed emulators');
          watchInstallAll();
        } catch(error) { notify(error.message); }
      };
      document.querySelectorAll('[data-update-emulator]').forEach(button => button.onclick = async () => {
        try { await api('/api/emulators/update',{method:'POST',body:JSON.stringify({app_id:button.dataset.updateEmulator})}); notify('Updating emulator'); watchEmulator(button.dataset.updateEmulator); } catch(error) { notify(error.message); }
      });
      document.querySelectorAll('[data-open-emulator]').forEach(button => button.onclick = async () => {
        try { await api('/api/emulators/open',{method:'POST',body:JSON.stringify({app_id:button.dataset.openEmulator})}); notify('Emulator launched'); } catch(error) { notify(error.message); }
      });
      document.querySelectorAll('[data-emulator]').forEach(button => button.onclick = async () => {
        const emulator = emulators.find(item => item.app_id === button.dataset.emulator);
        if (emulator.installed) {
          const current = new Set($('profilesText').value.split('\n').map(line => line.split('=',1)[0].trim()));
          const lines = Object.entries(emulator.profiles).filter(([platform]) => !current.has(platform)).map(([platform,command]) => `${platform} = ${command}`);
          $('profilesText').value = [$('profilesText').value,...lines].filter(Boolean).join('\n');
          notify(lines.length ? 'Profiles added. Save to apply them.' : 'Those profiles are already configured');
          return;
        }
        try {
          await api('/api/emulators/install',{method:'POST',body:JSON.stringify({app_id:emulator.app_id})});
          notify(`Installing ${emulator.name}`);
          watchEmulator(emulator.app_id);
        } catch(error) { notify(error.message); }
      });
    }
    async function watchEmulator(appId) {
      try {
        const result = await api('/api/emulators');
        renderEmulators(result.emulators);
        const emulator = result.emulators.find(item => item.app_id === appId);
        if (emulator?.job.state === 'installing') return setTimeout(() => watchEmulator(appId), 1500);
        if (emulator?.job.state === 'done') {
          const profiles = await api('/api/profiles');
          $('profilesText').value = Object.entries(profiles.profiles).sort().map(([platform,command]) => `${platform} = ${command}`).join('\n');
          notify(`${emulator.name} installed and configured`);
        } else if (emulator?.job.state === 'error') notify(emulator.job.error);
      } catch(error) { notify(error.message); }
    }
    async function watchInstallAll() {
      try {
        const result = await api('/api/emulators');
        renderEmulators(result.emulators);
        if (result.install_all?.state === 'installing') return setTimeout(watchInstallAll, 2000);
        if (result.install_all?.state === 'done') {
          const profiles = await api('/api/profiles');
          $('profilesText').value = Object.entries(profiles.profiles).sort().map(([platform,command]) => `${platform} = ${command}`).join('\n');
          notify(`Installed ${result.install_all.installed?.length || 0} emulator${result.install_all.installed?.length === 1 ? '' : 's'}`);
        } else if (result.install_all?.state === 'error') notify(result.install_all.error || 'Install all failed');
      } catch(error) { notify(error.message); }
    }
    let perfDraft = {};
    $('profilesForm').onsubmit = async event => {
      event.preventDefault();
      const profiles = {};
      $('profilesText').value.split('\n').forEach(line => {
        const split = line.indexOf('=');
        if (split > 0) profiles[line.slice(0,split).trim()] = line.slice(split + 1).trim();
      });
      try {
        const result = await api('/api/profiles',{method:'POST',body:JSON.stringify({profiles})});
        try { await api('/api/perf_profiles',{method:'POST',body:JSON.stringify({perf_profiles:perfDraft})}); } catch(error) { notify(error.message); }
        $('profilesDialog').close();
        notify(`${result.saved} emulator profiles saved`);
      } catch(error) { notify(error.message); }
    };
    async function loadTheme() {
      const result = await api(`/api/themes?platform=${encodeURIComponent(AppState.platform === 'all' ? '' : AppState.platform)}`);
      $('themeStylesheet').href = result.selected ? `/api/theme.css?name=${encodeURIComponent(result.selected)}&token=${encodeURIComponent(token)}` : '';
      return result;
    }
    async function openThemes() {
      try {
        const result = await loadTheme();
        const platforms = [...new Set(AppState.games.map(game => game.platform).filter(Boolean))].sort();
        $('themeScope').innerHTML = '<option value="">All platforms</option>' + platforms.map(name => `<option>${escapeHtml(name)}</option>`).join('');
        $('themeScope').value = AppState.platform === 'all' ? '' : AppState.platform;
        $('themeSelect').innerHTML = '<option value="">Default</option>' + result.themes.map(name => `<option>${escapeHtml(name)}</option>`).join('');
        $('themeSelect').value = result.selected;
        $('themeScope').onchange = async () => {
          const scoped = await api(`/api/themes?platform=${encodeURIComponent($('themeScope').value)}`);
          $('themeSelect').value = scoped.selected;
        };
        if (!$('themesDialog').open) $('themesDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    $('themesForm').onsubmit = async event => {
      event.preventDefault();
      try { await api('/api/themes/select',{method:'POST',body:JSON.stringify({name:$('themeSelect').value,platform:$('themeScope').value})}); $('themesDialog').close(); await loadTheme(); notify('Theme applied'); } catch(error) { notify(error.message); }
    };
    async function openAchievements() {
      try {
        const result = await api('/api/ra/settings');
        $('raUsername').value = result.username || '';
        $('raApiKey').value = '';
        $('raApiKey').required = !result.configured;
        $('raProfile').textContent = result.configured ? `${result.username} · ${result.points} points${result.motto ? ` · ${result.motto}` : ''}` : 'Get your web API key from your RetroAchievements settings.';
        $('achievementsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    $('achievementsForm').onsubmit = async event => {
      event.preventDefault();
      try {
        const result = await api('/api/ra/settings',{method:'POST',body:JSON.stringify({username:$('raUsername').value,api_key:$('raApiKey').value})});
        $('achievementsDialog').close();
        await refresh();
        notify(`Connected ${result.username}`);
      } catch(error) { notify(error.message); }
    };
    async function openPlugins() {
      try {
        const result = await api('/api/plugins');
        $('pluginList').innerHTML = result.plugins.length ? result.plugins.map(plugin => `<div class="emulator-item"><div><strong>${escapeHtml(plugin.name)} · ${escapeHtml(plugin.version)}</strong><small>${escapeHtml(plugin.id)} · ${escapeHtml(plugin.hooks.join(', ') || 'no hooks')}</small></div><button type="button" class="icon-button" data-toggle-plugin="${escapeHtml(plugin.id)}" data-enabled="${plugin.enabled}">${plugin.enabled ? 'Disable' : 'Enable'}</button><button type="button" class="playlist-delete" data-remove-plugin="${escapeHtml(plugin.id)}" aria-label="Remove ${escapeHtml(plugin.name)}">×</button></div>`).join('') : '<p class="description">No plugins installed.</p>';
        document.querySelectorAll('[data-toggle-plugin]').forEach(button => button.onclick = async () => {
          try {
            await api('/api/plugins/toggle',{method:'POST',body:JSON.stringify({id:button.dataset.togglePlugin,enabled:button.dataset.enabled !== 'true'})});
            await openPlugins();
          } catch(error) { notify(error.message); }
        });
        document.querySelectorAll('[data-remove-plugin]').forEach(button => button.onclick = async () => {
          if (!confirm(`Remove ${button.dataset.removePlugin}? A recoverable copy will be retained.`)) return;
          try { await api('/api/plugins/remove',{method:'POST',body:JSON.stringify({id:button.dataset.removePlugin})}); await openPlugins(); notify('Plugin removed'); } catch(error) { notify(error.message); }
        });
        if (!$('pluginsDialog').open) $('pluginsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    $('browsePluginCatalog').onclick = async () => {
      try {
        const result = await api('/api/plugins/catalog');
        const entry = result.catalog?.[0];
        if (!entry) return notify('No community plugins are listed yet.');
        if (entry.local_only) return notify(`${entry.name}: install local plugin packages manually.`);
        await api('/api/plugins/catalog/install',{method:'POST',body:JSON.stringify({id:entry.id})});
        await openPlugins();
        notify(`${entry.name} installed from catalog`);
      } catch(error) { notify(error.message); }
    };
    $('installPlugin').onclick = async () => {
      const path = prompt('Absolute path of the plugin directory or ZIP package');
      if (!path) return;
      try {
        const result = await api('/api/plugins/install',{method:'POST',body:JSON.stringify({path})});
        await openPlugins();
        notify(`${result.plugin.name} ${result.plugin.updated ? 'updated' : 'installed'}`);
      } catch(error) { notify(error.message); }
    };
    async function openMediaManager() {
      if (!$('mediaManagerDialog').open) $('mediaManagerDialog').showModal();
      try {
        const audit = await api(`/api/media/audit?platform=${encodeURIComponent(AppState.platform)}`);
        $('mediaAudit').innerHTML = `<h3>${AppState.platform === 'all' ? 'Entire library' : escapeHtml(AppState.platform)}</h3><div class="facts">${fact('Games',audit.games)}${fact('Database matched',audit.matched)}${fact('Missing box front',audit.missing_cover)}${fact('Missing background',audit.missing_background)}${fact('Missing screenshots',audit.missing_screenshots)}${fact('Missing box back',audit.missing_box_back)}${fact('Missing box spine',audit.missing_box_spine)}${fact('Missing 3D box',audit.missing_box_3d)}${fact('Missing clear logo',audit.missing_clear_logo)}${fact('Missing fanart',audit.missing_fanart)}${fact('Missing banner',audit.missing_banner)}${fact('Missing icon',audit.missing_icon)}${fact('Missing title screen',audit.missing_title_screen)}${fact('Missing cart front',audit.missing_cart_front)}${fact('Missing cart back',audit.missing_cart_back)}${fact('Missing disc',audit.missing_disc)}${fact('Missing ads / flyers',audit.missing_advertisement)}${fact('Missing manual',audit.missing_manual)}</div>`;
        const status = await api('/api/media/bulk/status');
        renderBulkMediaStatus(status.job);
      } catch(error) { notify(error.message); }
    }
    function renderBulkMediaStatus(job) {
      const manualMissing = job.manual_missing || 0;
      $('bulkMediaStatus').textContent = job.state === 'running' ? `${job.current || 0} of ${job.total || 0} · ${job.updated || 0} games updated` : job.state === 'done' ? `${job.updated || 0} games updated${manualMissing ? ` · ${manualMissing} had no manual in their archive` : ''}${job.errors?.length ? ` · ${job.errors.length} errors` : ''}` : '';
      $('startBulkMedia').disabled = job.state === 'running';
    }
    $('startBulkMedia').onclick = async () => {
      const media = [['cover','bulkCover'],['background','bulkBackground'],['screenshots','bulkScreenshots'],['box_back','bulkBoxBack'],['box_spine','bulkBoxSpine'],['box_3d','bulkBox3d'],['clear_logo','bulkClearLogo'],['fanart','bulkFanart'],['banner','bulkBanner'],['icon','bulkIcon'],['title_screen','bulkTitleScreen'],['cart_front','bulkCartFront'],['cart_back','bulkCartBack'],['disc','bulkDisc'],['advertisement','bulkAdvertisement'],['manual','bulkManual']].filter(([,id]) => $(id).checked).map(([name]) => name);
      try {
        await api('/api/media/bulk',{method:'POST',body:JSON.stringify({platform,media,overwrite:$('bulkOverwrite').checked})});
        watchBulkMedia();
      } catch(error) { notify(error.message); }
    };
    async function watchBulkMedia() {
      try {
        const result = await api('/api/media/bulk/status');
        renderBulkMediaStatus(result.job);
        if (result.job.state === 'running') return setTimeout(watchBulkMedia, 1200);
        await refresh();
        await openMediaManager();
        notify('Bulk media download finished');
      } catch(error) { notify(error.message); }
    }
    $('importTheme').onclick = async () => {
      const path = prompt('Enter the absolute path of a CSS theme file.');
      if (!path) return;
      try { await api('/api/themes/import',{method:'POST',body:JSON.stringify({path})}); await openThemes(); notify('Theme imported'); } catch(error) { notify(error.message); }
    };
    async function renderJobsPanel() {
      try {
        const result = await api('/api/jobs');
        const active = Object.entries(result.jobs || {}).filter(([, job]) => job.state === 'running' || job.state === 'queued');
        const finished = (result.history || []).slice(0, 5);
        if (!active.length && !finished.length) { $('jobsPanel').hidden = true; return; }
        const stateClass = state => state === 'error' ? 'danger' : (state === 'running' || state === 'queued') ? 'active' : '';
        const rows = [
          ...active.map(([name, job]) => `<div class="job-row"><strong>${escapeHtml(name)}</strong><span class="badge ${stateClass(job.state)}">${escapeHtml(job.state)}</span>${job.error ? `<small>${escapeHtml(job.error)}</small>` : ''}</div>`),
          ...finished.map(job => `<div class="job-row"><strong>${escapeHtml(job.name)}</strong><span class="badge ${stateClass(job.state)}">${escapeHtml(job.state)}</span><small>${job.duration_seconds}s ago-finish</small>${job.error ? `<small>${escapeHtml(job.error)}</small>` : ''}</div>`),
        ];
        $('jobsPanel').innerHTML = `<h3>Background jobs</h3><div class="facts">${rows.join('')}</div>`;
        $('jobsPanel').hidden = false;
      } catch (error) { /* jobs view is best-effort; never block the audit */ }
    }
    async function health() {
      try {
        const result = await api('/api/health',{method:'POST',body:'{}'});
        $('healthSummary').innerHTML = `<h3>Audit summary</h3><div class="facts">${fact('Games',result.games)}${fact('Missing games',result.missing)}${fact('Duplicates',result.duplicates)}${fact('Missing box fronts',result.missing_media)}</div>`;
        $('healthIssues').innerHTML = result.issues.length ? result.issues.map(issue => `<button type="button" class="metadata-result icon-button" data-audit-game="${issue.id}"><div><strong>${escapeHtml(issue.game)}</strong><small>${escapeHtml(issue.type)} · ${escapeHtml(issue.detail)}</small></div></button>`).join('') : '<p class="description">No library issues found.</p>';
        document.querySelectorAll('[data-audit-game]').forEach(button => button.onclick = () => { AppState.selectedId = Number(button.dataset.auditGame); $('healthDialog').close(); render(); });
        $('dedupeButton').disabled = !result.duplicates;
        renderJobsPanel();
        if (!$('healthDialog').open) $('healthDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    $('dedupeButton').onclick = async () => {
      if (!confirm('Remove duplicate library entries? Game files will not be deleted.')) return;
      try { const result = await api('/api/health/dedupe',{method:'POST',body:'{}'}); await refresh(); await health(); notify(`${result.removed.length} duplicate entries removed`); } catch(error) { notify(error.message); }
    };
    async function savePreset() {
      const name = prompt('Filter preset name', AppState.activeFilterPreset || AppState.activePlaylist);
      if (!name?.trim()) return;
      const bigbox_quick = confirm('Pin this preset to Big Box quick-switch?');
      const rules = {
        platform: AppState.platform,
        platform_category: AppState.platformCategory,
        view: $('view').value,
        query: $('sidebarSearch').value.trim(),
        esrb: $('esrbFilter')?.value || '',
        progress: AppState.explorerRules.progress || ($('view').value === 'playing' ? 'Playing' : ''),
      };
      try {
        await api('/api/filter-presets',{method:'POST',body:JSON.stringify({name:name.trim(),rules,bigbox_quick})});
        AppState.activeFilterPreset = name.trim();
        AppState.activePlaylist = '';
        await refresh();
        notify('Filter preset saved');
      } catch(error) { notify(error.message); }
    }
    async function saveFilter() {
      const name = prompt('Playlist name', AppState.activePlaylist);
      if (!name?.trim()) return;
      const rules = {platform: AppState.platform,platform_category:AppState.platformCategory,view:$('view').value,query:$('sidebarSearch').value.trim(),esrb:$('esrbFilter')?.value || '',progress:AppState.explorerRules.progress || ''};
      try {
        await api('/api/playlists',{method:'POST',body:JSON.stringify({name:name.trim(),type:'filter',rules})});
        AppState.activePlaylist = name.trim();
        await refresh();
        notify('Playlist saved');
      } catch(error) { notify(error.message); }
    }
    async function saveManualPlaylist(name, members, parent = '', notes = '') {
      const cleanName = String(name || '').trim();
      if (!cleanName) return false;
      try {
        await api('/api/playlists',{method:'POST',body:JSON.stringify({name:cleanName,type:'manual',rules:{},members,parent,notes})});
        await refresh();
        notify('Manual playlist saved');
        return true;
      } catch(error) { notify(error.message); return false; }
    }
    async function createManualPlaylist(seedId = null) {
      const name = prompt('Manual playlist name');
      if (!name?.trim()) return;
      const parent = prompt('Parent playlist, optional', '') || '';
      await saveManualPlaylist(name, seedId === null ? [] : [seedId], parent);
    }
    async function addGamesToPlaylist(name, ids) {
      const playlist = playlistFor(name);
      if (!playlist) return;
      if (playlist.type !== 'manual') return notify('Filter playlists manage membership from their rules.');
      const members = [...new Set([...(playlist.members || []), ...ids.map(id => AppState.games.find(game => game.id === id)?.game_id || id)])];
      await saveManualPlaylist(name, members, playlist.parent, playlist.notes);
    }
    async function updateManualPlaylist(playlist, members) {
      await saveManualPlaylist(playlist.name, members, playlist.parent, playlist.notes);
      openPlaylists();
    }
    async function openPlaylists() {
      $('playlistManagerList').innerHTML = AppState.playlists.length ? AppState.playlists.map(item => {
        const members = (item.members || []).map(value => AppState.games.find(game => String(game.game_id) === String(value))).filter(Boolean);
        const memberRows = item.type === 'manual' ? `<div class="member-list">${members.map((game,index) => `<div class="member-row"><span>${escapeHtml(game.name)}</span><button type="button" class="icon-button" data-playlist-name="${escapeHtml(item.name)}" data-playlist-index="${index}" data-playlist-direction="up">↑</button><button type="button" class="icon-button" data-playlist-name="${escapeHtml(item.name)}" data-playlist-index="${index}" data-playlist-direction="down">↓</button></div>`).join('') || '<span class="description">No games yet.</span>'}</div>` : '';
        return `<div class="playlist-manager-item"><div><strong>${escapeHtml(item.name)}</strong><small>${item.type === 'manual' ? `${members.length} ordered games` : 'Filter playlist'}${item.parent ? ` · under ${escapeHtml(item.parent)}` : ''}</small>${memberRows}</div><div class="playlist-manager-actions"><button type="button" class="icon-button" data-edit-playlist="${escapeHtml(item.name)}">Edit</button><button type="button" class="playlist-delete" data-manager-delete-playlist="${escapeHtml(item.name)}" aria-label="Delete ${escapeHtml(item.name)}">×</button></div></div>`;
      }).join('') : '<p class="description">No playlists yet.</p>';
      document.querySelectorAll('[data-manager-delete-playlist]').forEach(button => button.onclick = () => deletePlaylist(button.dataset.managerDeletePlaylist).then(openPlaylists));
      document.querySelectorAll('[data-edit-playlist]').forEach(button => button.onclick = () => {
        const item = playlistFor(button.dataset.editPlaylist);
        if (!item) return;
        const notes = prompt('Playlist notes', item.notes || '');
        if (notes === null) return;
        if (item.type === 'manual') updateManualPlaylist(item, item.members || []);
        else api('/api/playlists',{method:'POST',body:JSON.stringify({...item,notes})}).then(refresh).then(openPlaylists).catch(error => notify(error.message));
      });
      document.querySelectorAll('[data-playlist-index]').forEach(button => button.onclick = () => {
        const item = playlistFor(button.dataset.playlistName);
        const index = Number(button.dataset.playlistIndex);
        if (!item || item.type !== 'manual') return;
        const members = [...item.members];
        const target = index + (button.dataset.playlistDirection === 'up' ? -1 : 1);
        if (target < 0 || target >= members.length) return;
        [members[index],members[target]] = [members[target],members[index]];
        updateManualPlaylist(item, members);
      });
      if (!$('playlistsDialog').open) $('playlistsDialog').showModal();
    }
    async function createFilterPlaylist() { await saveFilter(); openPlaylists(); }
    async function openBackups() {
      try {
        const result = await api('/api/backups');
        $('backupList').innerHTML = result.backups.length ? result.backups.map(item => `<div class="backup-item"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.created || 'Unknown date')} · ${formatBytes(item.size)} · ${(item.items || []).join(', ') || 'Unknown contents'}</small></div><button type="button" class="icon-button" data-restore-backup="${escapeHtml(item.path)}" ${item.invalid ? 'disabled' : ''}>Restore</button></div>`).join('') : '<p class="description">No backups have been created yet.</p>';
        document.querySelectorAll('[data-restore-backup]').forEach(button => button.onclick = async () => {
          if (!confirm('Restore this backup? A safety copy of the current library will be created first.')) return;
          try { const result = await api('/api/backup/restore',{method:'POST',body:JSON.stringify({path:button.dataset.restoreBackup})}); await refresh(); notify(`Restored ${result.restored.join(', ')}`); } catch(error) { notify(error.message); }
        });
        if (!$('backupDialog').open) $('backupDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function createNamedBackup() {
      try { const result = await api('/api/backup/create',{method:'POST',body:JSON.stringify({items:['library','settings','media','plugins','themes'],keep:7})}); notify(`Backup saved to ${result.name}`); openBackups(); } catch(error) { notify(error.message); }
    }
    async function deletePlaylist(name) {
      if (!confirm(`Delete playlist "${name}"?`)) return;
      try {
        await api('/api/playlists/delete',{method:'POST',body:JSON.stringify({name})});
        if (AppState.activePlaylist === name) AppState.activePlaylist = '';
        await refresh();
        notify('Playlist deleted');
      } catch(error) { notify(error.message); }
    }
    async function backup() {
      try {
        const result = await api('/api/backup/create',{method:'POST',body:JSON.stringify({items:['library','settings','media','plugins','themes'],keep:7})});
        notify(`Backup saved to ${result.name}`);
      } catch(error) { notify(error.message); }
    }
    function bulkAction() {
      if (!AppState.bulkMode) {
        AppState.bulkMode = true;
        selectedIds.clear();
        renderGrid();
        notify('Select games, then choose Edit Selected');
      } else if (selectedIds.size) {
        $('bulkForm').reset();
        $('bulkCount').textContent = `${selectedIds.size} game${selectedIds.size === 1 ? '' : 's'} selected. Only supplied values will change.`;
        $('bulkDialog').showModal();
      } else {
        AppState.bulkMode = false;
        renderGrid();
      }
    }
    $('bulkForm').onsubmit = async event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      const changes = {};
      for (const field of ['platform','genre']) if (values[field].trim()) changes[field] = values[field].trim();
      if (values.progress) changes.progress = values.progress === '__clear' ? '' : values.progress;
      if (values.rating !== '') changes.rating = Number(values.rating);
      for (const field of ['favorite','hidden']) if (values[field]) changes[field] = values[field] === 'true';
      if (values.esrb) changes.esrb = values.esrb;
      try {
        const result = await api('/api/games/bulk-wizard',{method:'POST',body:JSON.stringify({ids:[...selectedIds],changes})});
        $('bulkDialog').close();
        selectedIds.clear();
        AppState.bulkMode = false;
        await refresh();
        notify(`${result.updated} games updated`);
      } catch(error) { notify(error.message); }
    };
    function openBigBox() {
      AppState.bigBoxFilter = 'all';
      AppState.bigBoxSort = 'title';
      AppState.bigBoxHybridQuery = '';
      if ($('bigBoxHybridSearch')) $('bigBoxHybridSearch').value = '';
      AppState.bigBoxGames = filteredBigBoxGames();
      if (!AppState.bigBoxGames.length) { notify('No games are available in the current view'); return; }
      AppState.bigBoxIndex = Math.max(0,AppState.bigBoxGames.findIndex(game => game.id === AppState.selectedId));
      AppState.bigBoxLastInput = performance.now();
      AppState.gamepadState = {};
      $('bigBox').hidden = false;
      $('bigBox').focus();
      const startup = $('bigBoxStartupVideo');
      if (startup && AppState.appSettings.bigbox_startup_video) {
        startup.src = AppState.appSettings.bigbox_startup_video;
        startup.hidden = false;
        startup.play().catch(() => {});
      } else if (startup) {
        startup.hidden = true;
        startup.pause();
        startup.removeAttribute('src');
      }
      renderBigBox();
      api('/api/bigbox/mode',{method:'POST',body:JSON.stringify({entering:true})}).catch(() => {});
      document.documentElement.requestFullscreen?.().catch(() => {});
      requestAnimationFrame(pollGamepads);
    }
    function closeBigBox() {
      stopScreenSaver();
      $('bigBoxMenu').hidden = true;
      $('bigBox').hidden = true;
      if ($('bigBoxStartupVideo')) { $('bigBoxStartupVideo').pause(); $('bigBoxStartupVideo').hidden = true; }
      api('/api/bigbox/mode',{method:'POST',body:JSON.stringify({entering:false})}).catch(() => {});
      if (document.fullscreenElement) document.exitFullscreen?.();
    }
    function filteredBigBoxGames() {
      let result = filteredGames().filter(game => !game.hide_in_bigbox);
      if (AppState.bigBoxPlatform !== 'all') result = result.filter(game => game.platform === AppState.bigBoxPlatform);
      if (AppState.bigBoxHybridQuery) {
        const query = AppState.bigBoxHybridQuery.toLowerCase();
        result = result.filter(game => [game.name,game.sort_title,game.genre,game.developer].join(' ').toLowerCase().includes(query));
      }
      if (AppState.bigBoxFilter === 'installed') result = result.filter(game => game.store_installed !== false && (game.path_exists || game.store_installed));
      if (AppState.bigBoxFilter === 'owned') result = result.filter(game => game.owned || game.store_catalog || game.gameyfin_id || game.steam_app_id || game.heroic_app_id || game.lutris_id);
      if (AppState.bigBoxFilter === 'favorites') result = result.filter(game => game.favorite);
      if (AppState.bigBoxFilter === 'playing') result = result.filter(game => ['Playing','Paused'].includes(game.progress));
      if (AppState.bigBoxFilter === 'completed') result = result.filter(game => ['Beaten','Completed','Mastered'].includes(game.progress));
      if (AppState.bigBoxRaFilter === 'matched') result = result.filter(game => game.ra_game_id);
      if (AppState.bigBoxRaFilter === 'unmatched') result = result.filter(game => !game.ra_game_id);
      result = [...result];
      if (AppState.bigBoxSort === 'rating') result.sort((a,b) => Number(b.rating || 0) - Number(a.rating || 0) || a.name.localeCompare(b.name));
      else if (AppState.bigBoxSort === 'recent') result.sort((a,b) => String(b.last_played || '').localeCompare(String(a.last_played || '')));
      else if (AppState.bigBoxSort === 'recent_activity') result.sort((a,b) => recentActivityValue(b) - recentActivityValue(a));
      else if (AppState.bigBoxSort === 'playtime') result.sort((a,b) => Number(b.playtime_seconds || 0) - Number(a.playtime_seconds || 0));
      else if (AppState.bigBoxSort === 'random') {
        for (let index = result.length - 1; index > 0; index--) {
          const swap = Math.floor(Math.random() * (index + 1));
          [result[index],result[swap]] = [result[swap],result[index]];
        }
      } else result.sort((a,b) => String(a.sort_title || a.name).localeCompare(String(b.sort_title || b.name)));
      return result;
    }
    function openBigBoxMenu() {
      $('bigBoxFilter').value = AppState.bigBoxFilter;
      $('bigBoxSort').value = AppState.bigBoxSort;
      $('bigBoxRaFilter').value = AppState.bigBoxRaFilter;
      $('bigBoxMenu').hidden = false;
      $('bigBoxFilter').focus();
    }
    function closeBigBoxMenu() {
      $('bigBoxMenu').hidden = true;
      $('bigBox').focus();
      AppState.bigBoxLastInput = performance.now();
    }
    function applyBigBoxMenu() {
      const currentId = AppState.bigBoxGames[AppState.bigBoxIndex]?.id;
      AppState.bigBoxFilter = $('bigBoxFilter').value;
      AppState.bigBoxSort = $('bigBoxSort').value;
      AppState.bigBoxRaFilter = $('bigBoxRaFilter').value;
      const quickPreset = $('bigBoxQuickPreset')?.value;
      if (quickPreset) {
        const preset = AppState.filterPresets.find(item => item.name === quickPreset);
        if (preset?.rules) {
          AppState.bigBoxFilter = preset.rules.view === 'favorites' ? 'favorites' : preset.rules.view === 'installed' ? 'installed' : preset.rules.view === 'owned' ? 'owned' : preset.rules.view === 'playing' ? 'playing' : preset.rules.view === 'completed' ? 'completed' : AppState.bigBoxFilter;
        }
      }
      AppState.bigBoxGames = filteredBigBoxGames();
      if (!AppState.bigBoxGames.length) { notify('No games match those Big Box filters'); return; }
      AppState.bigBoxIndex = Math.max(0,AppState.bigBoxGames.findIndex(game => game.id === currentId));
      closeBigBoxMenu();
      renderBigBox();
    }
    function moveBigBox(change) {
      AppState.bigBoxIndex = (AppState.bigBoxIndex + change % AppState.bigBoxGames.length + AppState.bigBoxGames.length) % AppState.bigBoxGames.length;
      AppState.selectedId = AppState.bigBoxGames[AppState.bigBoxIndex].id;
      AppState.bigBoxLastInput = performance.now();
      renderBigBox();
    }
    function renderBigBox() {
      const game = AppState.bigBoxGames[AppState.bigBoxIndex];
      if (!game) return;
      $('bigBoxCounter').textContent = `${AppState.bigBoxIndex + 1} / ${AppState.bigBoxGames.length}`;
      const hint = AppState.appSettings.controller_prompt_hint || 'A Play · B Back · M Menu';
      navigator.getBattery?.().then(status => {
        if (!$('bigBoxStatus')) return;
        $('bigBoxStatus').innerHTML = status ? `<strong>${Math.round(status.level * 100)}%</strong> battery · ${escapeHtml(hint)}` : escapeHtml(hint);
      }).catch(() => { if ($('bigBoxStatus')) $('bigBoxStatus').textContent = hint; });
      const mode = AppState.appSettings.bigbox_mode || 'stage';
      if ($('bigBoxHybridSearch')) $('bigBoxHybridSearch').hidden = mode !== 'hybrid';
      const playLabel = game.gameyfin_id && !game.store_installed
        ? '⬇ INSTALL'
        : (AppState.appSettings.dynamic_play_button === false ? '▶ PLAY' : game.versions.length ? '▶ PLAY DEFAULT' : game.applications.length ? '▶ PLAY GAME' : '▶ PLAY');
      if (mode === 'hybrid') {
        const platforms = [...new Set(AppState.bigBoxGames.map(item => item.platform || 'Unspecified'))].sort();
        if (AppState.bigBoxPlatform === 'all' && platforms.length) AppState.bigBoxPlatform = platforms[0];
        const platformGames = AppState.bigBoxGames.filter(item => (item.platform || 'Unspecified') === AppState.bigBoxPlatform);
        if (platformGames.length && !platformGames.some(item => item.id === game.id)) {
          AppState.bigBoxIndex = AppState.bigBoxGames.findIndex(item => item.id === platformGames[0].id);
          return renderBigBox();
        }
        $('bigBoxStage').className = 'bigbox-stage bigbox-hybrid';
        const canAct = (game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed);
        const ownedLabel = game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : '';
        const uninstallBtn = game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:10px">Uninstall</button>' : '';
        $('bigBoxStage').innerHTML = `<div class="bigbox-platforms">${platforms.map(name => `<button class="bigbox-platform ${AppState.bigBoxPlatform === name ? 'active' : ''}" data-bigbox-AppState.platform ="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join('')}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${ownedLabel}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${canAct ? '' : 'disabled'}>${playLabel}</button>${uninstallBtn}</div>`;
        document.querySelectorAll('[data-bigbox-platform]').forEach(button => button.onclick = () => { AppState.bigBoxPlatform = button.dataset.bigboxPlatform; AppState.bigBoxIndex = 0; renderBigBox(); });
      } else if (mode === 'coverflow') {
        $('bigBoxStage').className = 'bigbox-stage';
        const canAct = (game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed);
        const ownedLabel = game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : '';
        const uninstallBtn = game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:10px">Uninstall</button>' : '';
        $('bigBoxStage').innerHTML = `<div class="coverflow-strip">${AppState.bigBoxGames.map((item,index) => `<button class="coverflow-card jewel-case ${index === AppState.bigBoxIndex ? 'active' : ''}" data-coverflow="${index}" aria-label="Open ${escapeHtml(item.name)}">${item.has_cover ? `<img src="${media(item,'cover')}" alt="" loading="lazy" decoding="async">` : `<div class="cover-title">${escapeHtml(item.name)}</div>`}</button>`).join('')}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${ownedLabel}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${canAct ? '' : 'disabled'}>${playLabel}</button>${uninstallBtn}</div>`;
        document.querySelectorAll('[data-coverflow]').forEach(button => button.onclick = () => { AppState.bigBoxIndex = Number(button.dataset.coverflow); AppState.selectedId = AppState.bigBoxGames[AppState.bigBoxIndex].id; renderBigBox(); });
      } else {
        $('bigBoxStage').className = 'bigbox-stage';
        $('bigBoxStage').innerHTML = `<div class="bigbox-cover${game.has_cover ? '' : ' no-image'}">${game.has_cover ? `<img src="${media(game,'cover')}" alt="">` : escapeHtml(game.name)}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : ''}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${(game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed) ? '' : 'disabled'}>${playLabel}</button>${game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:10px">Uninstall</button>' : ''}</div>`;
      }
      $('bigBoxPlay').onclick = () => {
        if (game.gameyfin_id && !game.store_installed) installGameyfin(game);
        else launch(game.id);
      };
      if ($('bigBoxUninstall')) $('bigBoxUninstall').onclick = () => uninstallGameyfin(game);
      applyLibraryMusic();
    }
    function applyLibraryMusic() {
      const path = AppState.appSettings.library_music;
      if (!path) { if (AppState.libraryBgm) { AppState.libraryBgm.pause(); AppState.libraryBgm = null; } return; }
      if (!AppState.libraryBgm) { AppState.libraryBgm = new Audio(); AppState.libraryBgm.loop = true; AppState.libraryBgm.volume = AppState.appSettings.video_bgm_mix ? 0.35 : 0.6; }
      if (AppState.libraryBgm.src !== location.origin + path && !AppState.libraryBgm.src.endsWith(path)) AppState.libraryBgm.src = path;
      if ($('bigBox').hidden) { AppState.libraryBgm.pause(); return; }
      AppState.libraryBgm.play().catch(() => {});
    }
    function openBigBoxPause() {
      const session = AppState.runningGames[0];
      if (!session) return openSessions();
      const game = AppState.games.find(item => item.id === session.game_id);
      $('bigBoxPauseTitle').textContent = session.game;
      $('bigBoxPauseMeta').textContent = `${session.paused ? 'Paused' : 'Running'} · started ${session.started.replace('T',' ')}`;
      $('bigBoxPauseActions').innerHTML = `<button class="primary" data-pause-action="${session.launch_id}:${session.paused ? 'resume' : 'pause'}">${session.paused ? 'Resume' : 'Pause'}</button><button class="icon-button" data-pause-action="${session.launch_id}:stop">Exit game</button>${game?.documents.map((item,index) => `<button class="icon-button" data-pause-doc="${game.id}:${index}">Read ${escapeHtml(item.name)}</button>`).join('') || ''}${AppState.raConfigured ? `<button class="icon-button" id="pauseAchievements">Achievements</button>` : ''}`;
      document.querySelectorAll('[data-pause-action]').forEach(button => button.onclick = async () => {
        const [launch_id,action] = button.dataset.pauseAction.split(':');
        await api('/api/session/control',{method:'POST',body:JSON.stringify({launch_id,action})});
        $('bigBoxPause').hidden = true;
        openBigBoxPause();
      });
      document.querySelectorAll('[data-pause-doc]').forEach(button => button.onclick = () => {
        const [id,index] = button.dataset.pauseDoc.split(':');
        const docGame = AppState.games.find(item => item.id === Number(id));
        if (!docGame) return;
        openReader(docGame, Number(index));
      });
      if ($('pauseAchievements')) $('pauseAchievements').onclick = () => { if (game) loadAchievements(game.id); };
      $('bigBoxPause').hidden = false;
    }
    async function openDiscovery() {
      try {
        const result = await api('/api/discovery');
        const labels = {recently_added:'Recently added',never_played:'Never played',continue_playing:'Continue playing',highly_rated:'Highly rated',random_picks:'Random picks',short_sessions:'Short sessions'};
        $('discoveryContent').innerHTML = Object.entries(labels).map(([key,label]) => {
          const ids = result[key] || [];
          const items = ids.map(id => AppState.games.find(game => game.id === id)).filter(Boolean);
          return `<div class="discovery-section"><h3>${label}</h3><div class="discovery-row">${items.length ? items.map(game => `<button class="related-game" data-discovery="${game.id}">${escapeHtml(game.name)}</button>`).join('') : '<span class="description">No matches yet.</span>'}</div></div>`;
        }).join('');
        document.querySelectorAll('[data-discovery]').forEach(button => button.onclick = () => { AppState.selectedId = Number(button.dataset.discovery); $('discoveryDialog').close(); render(); });
        $('discoveryDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function loadStorefrontCatalog() {
      const source = $('storefrontSource').value;
      try {
        const result = await api(`/api/storefront/catalog?source=${encodeURIComponent(source)}`);
        $('storefrontCatalog').innerHTML = result.catalog.length ? result.catalog.slice(0, 200).map(item => `<div class="storefront-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.source)} · ${item.installed ? 'Installed' : 'Not installed'}</small></div><span>${escapeHtml(item.id)}</span></div>`).join('') : '<p class="description">No storefront entries were found.</p>';
      } catch(error) { notify(error.message); }
    }
    async function importStorefrontCatalog(uninstalledOnly) {
      const source = $('storefrontSource').value;
      try {
        const result = await api('/api/storefront/import',{method:'POST',body:JSON.stringify({source,uninstalled_only:uninstalledOnly,installed_only:!uninstalledOnly})});
        await refresh();
        notify(`${result.added} ${uninstalledOnly ? 'uninstalled catalog' : 'installed'} entr${result.added === 1 ? 'y' : 'ies'} added`);
      } catch(error) { notify(error.message); }
    }
    function openStorefronts() {
      const settings = AppState.appSettings.storefront_auto_import || {};
      $('storefrontAutoImportSteam').checked = Boolean(settings.steam);
      $('storefrontAutoImportHeroic').checked = Boolean(settings.heroic);
      $('storefrontAutoImportLutris').checked = Boolean(settings.lutris);
      if ($('storefrontAutoImportGameyfin')) $('storefrontAutoImportGameyfin').checked = Boolean(settings.gameyfin);
      if ($('storefrontGameyfinUrl')) $('storefrontGameyfinUrl').value = AppState.appSettings.gameyfin_url || '';
      if ($('storefrontGameyfinUsername')) $('storefrontGameyfinUsername').value = AppState.appSettings.gameyfin_username || '';
      if ($('storefrontGameyfinPassword')) $('storefrontGameyfinPassword').value = '';
      if ($('storefrontGameyfinInstallDir')) $('storefrontGameyfinInstallDir').value = AppState.appSettings.gameyfin_install_dir || '';
      if ($('storefrontGameyfinStatus')) $('storefrontGameyfinStatus').textContent = AppState.appSettings.gameyfin_url ? `Configured · ${AppState.appSettings.gameyfin_url}` : '';
      $('storefrontCatalog').innerHTML = '';
      $('storefrontDialog').showModal();
    }
    async function saveStorefrontSettings() {
      AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectStorefrontSettings())});
      notify('Storefront settings saved');
    }
    async function watchGameyfinInstall(gameyfinId, attempts = 1200) {
      const job = await api(`/api/gameyfin/install/status?gameyfin_id=${encodeURIComponent(gameyfinId)}`);
      if (job.state === 'installing') {
        if (attempts <= 0) throw new Error('Gameyfin install timed out');
        await new Promise(resolve => setTimeout(resolve, 1500));
        return watchGameyfinInstall(gameyfinId, attempts - 1);
      }
      if (job.state === 'error') throw new Error(job.error || 'Gameyfin install failed');
      if (job.state !== 'done') throw new Error('Gameyfin install did not complete');
    }
    async function installGameyfin(game) {
      try {
        notify(`Installing ${game.name} from Gameyfin...`);
        await api('/api/gameyfin/install',{method:'POST',body:JSON.stringify({gameyfin_id:game.gameyfin_id,library_id:game.id})});
        await watchGameyfinInstall(game.gameyfin_id);
        await refresh();
        AppState.bigBoxGames = filteredBigBoxGames();
        if (!$('bigBox').hidden) renderBigBox();
        else renderDetails();
        notify(`${game.name} installed`);
      } catch(error) { notify(error.message); }
    }
    async function uninstallGameyfin(game) {
      if (!confirm(`Remove the local Gameyfin install for ${game.name}? The server copy stays intact.`)) return;
      try {
        await api('/api/gameyfin/uninstall',{method:'POST',body:JSON.stringify({id:game.id})});
        await refresh();
        AppState.bigBoxGames = filteredBigBoxGames();
        if (!$('bigBox').hidden) renderBigBox();
        else renderDetails();
        notify(`${game.name} uninstalled locally`);
      } catch(error) { notify(error.message); }
    }
    async function ludusaviAction(id, action) {
      try {
        const result = await api('/api/save-tools/ludusavi',{method:'POST',body:JSON.stringify({id,action})});
        notify(`Ludusavi ${action} ${result.ok ? 'finished' : 'reported issues'}`);
      } catch(error) { notify(error.message); }
    }
    async function hoardAction(id, action) {
      try {
        await api('/api/save-tools/hoard',{method:'POST',body:JSON.stringify({id,action})});
        notify(`Hoard ${action} finished`);
      } catch(error) { notify(error.message); }
    }
    async function captureScreenshot(id) { try { const result = await api('/api/screenshot',{method:'POST',body:JSON.stringify({id})}); await refresh(); notify(`Screenshot saved to ${result.path}`); } catch(error) { notify(error.message); } }
    async function downloadBezel(platform) { if (!platform) return notify('Select a game with a platform first'); try { const result = await api('/api/bezels/download',{method:'POST',body:JSON.stringify({platform})}); notify(`Bezels downloaded to ${result.path}`); } catch(error) { notify(error.message); } }
    async function runStartupStorefrontImports() {
      const settings = AppState.appSettings.storefront_auto_import || {};
      if (settings.steam) await api('/api/import/steam',{method:'POST',body:'{}'}).catch(() => {});
      if (settings.heroic) await api('/api/import/heroic',{method:'POST',body:'{}'}).catch(() => {});
      if (settings.lutris) await api('/api/import/lutris',{method:'POST',body:'{}'}).catch(() => {});
      if (settings.gameyfin) await api('/api/storefront/import',{method:'POST',body:JSON.stringify({source:'gameyfin'})}).catch(() => {});
    }
    function startScreenSaver() {
      if (!AppState.bigBoxGames.length || $('bigBox').hidden) return;
      AppState.screenSaverGame = AppState.bigBoxGames[Math.floor(Math.random() * AppState.bigBoxGames.length)];
      $('screenSaver').style.backgroundImage = AppState.screenSaverGame.has_background ? `url('${media(AppState.screenSaverGame,'background')}')` : AppState.screenSaverGame.has_cover ? `url('${media(AppState.screenSaverGame,'cover')}')` : '';
      $('screenSaverTitle').textContent = AppState.screenSaverGame.name;
      $('screenSaverPlatform').textContent = AppState.screenSaverGame.platform || '';
      if (AppState.screenSaverGame.has_video) {
        $('screenSaverVideo').src = media(AppState.screenSaverGame,'video');
        $('screenSaverVideo').play().catch(() => {});
      } else {
        $('screenSaverVideo').removeAttribute('src');
        $('screenSaverVideo').load();
      }
      $('screenSaver').hidden = false;
      clearTimeout(startScreenSaver.timer);
      startScreenSaver.timer = setTimeout(startScreenSaver, 15000);
    }
    function stopScreenSaver() {
      clearTimeout(startScreenSaver.timer);
      $('screenSaver').hidden = true;
      $('screenSaverVideo').pause();
      $('screenSaverVideo').removeAttribute('src');
      AppState.screenSaverGame = null;
      AppState.bigBoxLastInput = performance.now();
    }
    async function favoriteBigBox() {
      const id = AppState.bigBoxGames[AppState.bigBoxIndex]?.id;
      if (id === undefined) return;
      try {
        await api('/api/favorite',{method:'POST',body:JSON.stringify({id})});
        await refresh();
        AppState.bigBoxGames = filteredBigBoxGames();
        if (!AppState.bigBoxGames.length) { closeBigBox(); notify('The current view is now empty'); return; }
        AppState.bigBoxIndex = Math.max(0,AppState.bigBoxGames.findIndex(game => game.id === id));
        renderBigBox();
      } catch(error) { notify(error.message); }
    }
    function pollGamepads() {
      if ($('bigBox').hidden) return;
      const pad = navigator.getGamepads?.()[0];
      if (pad) {
        const mapping = {...defaultControllerMap,...AppState.appSettings.controller_map};
        const pressed = action => Boolean(pad.buttons[mapping[action]]?.pressed);
        const current = {left:pad.buttons[14]?.pressed || pad.axes[0] < -.6,right:pad.buttons[15]?.pressed || pad.axes[0] > .6,up:pad.buttons[12]?.pressed || pad.axes[1] < -.6,down:pad.buttons[13]?.pressed || pad.axes[1] > .6,play:pressed('play'),back:pressed('back'),favorite:pressed('favorite'),random:pressed('random'),pageLeft:pressed('page_left'),pageRight:pressed('page_right'),pause:pressed('pause'),menu:pressed('menu')};
        const edge = action => current[action] && !AppState.gamepadState[action];
        if (!$('screenSaver').hidden && Object.keys(current).some(edge)) {
          const game = AppState.screenSaverGame;
          stopScreenSaver();
          if (edge('play') && game) launch(game.id);
          AppState.gamepadState = current;
          requestAnimationFrame(pollGamepads);
          return;
        }
        if (!$('bigBoxMenu').hidden) {
          const selects = [$('bigBoxFilter'),$('bigBoxSort')];
          let active = Math.max(0,selects.indexOf(document.activeElement));
          if (edge('up') || edge('down')) selects[active ? 0 : 1].focus();
          if (edge('left') || edge('right')) {
            const select = selects[active], change = edge('left') ? -1 : 1;
            select.selectedIndex = (select.selectedIndex + change + select.options.length) % select.options.length;
          }
          if (edge('play')) applyBigBoxMenu();
          if (edge('back') || edge('menu')) closeBigBoxMenu();
        } else {
          if (edge('left') || edge('up')) moveBigBox(-1);
          if (edge('right') || edge('down')) moveBigBox(1);
          if (edge('play')) launch(AppState.bigBoxGames[AppState.bigBoxIndex].id);
          if (edge('back')) closeBigBox();
          if (edge('favorite')) favoriteBigBox();
          if (edge('random')) { AppState.bigBoxIndex = Math.floor(Math.random() * AppState.bigBoxGames.length); renderBigBox(); }
          if (edge('pageLeft')) moveBigBox(-10);
          if (edge('pageRight')) moveBigBox(10);
          if (edge('pause') && AppState.runningGames.length) openBigBoxPause();
          if (edge('menu')) openBigBoxMenu();
        }
        if (Object.keys(current).some(edge)) AppState.bigBoxLastInput = performance.now();
        AppState.gamepadState = current;
      }
      if ($('screenSaver').hidden && $('bigBoxMenu').hidden && (() => {
        const delay = Number(AppState.appSettings.attract_mode_seconds ?? AppState.appSettings.screensaver_seconds ?? 0);
        return delay && performance.now() - AppState.bigBoxLastInput >= delay * 1000;
      })()) startScreenSaver();
      requestAnimationFrame(pollGamepads);
    }
    let searchTimer = null;
    $('sidebarSearch').oninput = () => { AppState.activePlaylist = ''; clearTimeout(searchTimer); searchTimer = setTimeout(() => { renderPlaylists(); renderGrid(); }, 150); };
    $('view').onchange = () => { AppState.activePlaylist = ''; renderPlaylists(); renderGrid(); };
    $('sort').onchange = renderGrid;
    if ($('esrbFilter')) $('esrbFilter').onchange = renderGrid;
    const libraryPaneElement = document.querySelector('main.library');
    if (libraryPaneElement) {
      libraryPaneElement.addEventListener('scroll', () => {
        const top = libraryPaneElement.scrollTop;
        if (gridRowHeight && Math.abs(top - gridScrollTop) < gridRowHeight) return;
        gridScrollTop = top;
        renderGrid();
      }, { passive: true });
      window.addEventListener('resize', () => { gridRowHeight = 0; renderGrid(); });
    }
    $('viewToggleButton').onclick = () => {
      AppState.appSettings.library_view = (AppState.appSettings.library_view || 'grid') === 'list' ? 'grid' : 'list';
      if ($('libraryViewSetting')) $('libraryViewSetting').value = AppState.appSettings.library_view;
      AppState.persist();
      applyLocaleStrings();
      renderGrid();
    };
    async function importDroppedFolder(folder) {
      try {
        const preview = await api('/api/import',{method:'POST',body:JSON.stringify({folder,recommend:true})});
        const chosen = {};
        for (const [platformName, items] of Object.entries(preview.recommendations || {})) {
          if (!items?.length) continue;
          if (items.length === 1) { chosen[platformName] = items[0].app_id; continue; }
          const labels = items.map((item,index) => `${index + 1}. ${item.name}`).join('\n');
          const pick = prompt(`Multiple emulators for ${platformName}:\n${labels}\nEnter number to install, or leave blank to skip`, '1');
          const index = Number(pick) - 1;
          if (pick && items[index]) chosen[platformName] = items[index].app_id;
        }
        const result = Object.keys(chosen).length
          ? await api('/api/import/wizard',{method:'POST',body:JSON.stringify({folder,chosen_emulators:chosen})})
          : preview;
        await refresh();
        notify(`${result.added} games imported${result.installed?.length ? ` · installed ${result.installed.length} emulator(s)` : ''}`);
      } catch(error) { notify(error.message); }
    }
    if ($('dropZone')) {
      ['dragenter','dragover'].forEach(name => $('dropZone').addEventListener(name, event => { event.preventDefault(); $('dropZone').classList.add('active'); }));
      $('dropZone').addEventListener('dragleave', () => $('dropZone').classList.remove('active'));
      $('dropZone').addEventListener('drop', event => {
        event.preventDefault();
        $('dropZone').classList.remove('active');
        const names = [...event.dataTransfer.items].map(item => item.getAsFile?.()?.name).filter(Boolean);
        const hint = names.length ? `\n\nDropped: ${names.slice(0, 3).join(', ')}` : '';
        const folder = prompt(`Enter the absolute path of the folder to import.${hint}`);
        if (folder) importDroppedFolder(folder.trim());
      });
      $('dropZone').onclick = () => importFolder();
    }
    if ($('bigBoxHybridSearch')) {
      $('bigBoxHybridSearch').oninput = () => {
        AppState.bigBoxHybridQuery = $('bigBoxHybridSearch').value.trim();
        AppState.bigBoxIndex = 0;
        AppState.bigBoxGames = filteredBigBoxGames();
        renderBigBox();
      };
    }
    if ($('applyMediaPack')) $('applyMediaPack').onclick = async () => {
      try {
        const catalog = await api('/api/premium/media-packs');
        const packs = catalog.packs || [];
        if (!packs.length) return notify('No bundled media packs are available');
        const labels = packs.map((pack,index) => `${index + 1}. ${pack.name}${pack.active ? ' (active)' : ''}`).join('\n');
        const pick = prompt(`Choose a bundled media pack:\n${labels}`, '1');
        const pack = packs[Number(pick) - 1];
        if (!pack) return;
        const result = await api('/api/premium/media-packs/apply',{method:'POST',body:JSON.stringify({id:pack.id})});
        AppState.appSettings = result.settings || appSettings;
        $('mediaPackStatus').textContent = (appSettings.media_packs || []).filter(item => item.active).map(item => item.name).join(', ');
        notify(`Applied ${pack.name}`);
      } catch(error) { notify(error.message); }
    };
    $('libraryButton').onclick = () => { AppState.activePlaylist = ''; AppState.platform = 'all'; $('view').value = 'all'; $('sidebarSearch').value = ''; AppState.selectedId = null; render(); loadTheme(); };
    $('discoveryButton').onclick = openDiscovery;
    $('storefrontButton').onclick = openStorefronts;
    $('closeDiscovery').onclick = $('doneDiscovery').onclick = () => $('discoveryDialog').close();
    $('closeStorefront').onclick = () => $('storefrontDialog').close();
    $('saveStorefront').onclick = saveStorefrontSettings;
    $('importSteamStore').onclick = () => { $('storefrontDialog').close(); importSteam(); };
    $('importHeroicStore').onclick = () => { $('storefrontDialog').close(); importHeroic(); };
    $('importLutrisStore').onclick = () => { $('storefrontDialog').close(); importLutris(); };
    if ($('importGameyfinStore')) $('importGameyfinStore').onclick = async () => {
      try {
        await saveStorefrontSettings();
        const result = await api('/api/storefront/import',{method:'POST',body:JSON.stringify({source:'gameyfin'})});
        await refresh();
        notify(`${result.added} Gameyfin games imported · ${result.found} owned`);
      } catch(error) { notify(error.message); }
    };
    if ($('testGameyfin')) $('testGameyfin').onclick = async () => {
      try {
        const result = await api('/api/gameyfin/test',{method:'POST',body:JSON.stringify({
          gameyfin_url:$('storefrontGameyfinUrl').value.trim(),
          gameyfin_username:$('storefrontGameyfinUsername').value.trim(),
          gameyfin_password:$('storefrontGameyfinPassword').value,
        })});
        $('storefrontGameyfinStatus').textContent = `Connected · ${result.games} games · ${result.providers.length} download provider${result.providers.length === 1 ? '' : 's'}`;
        notify('Gameyfin connection ok');
      } catch(error) { $('storefrontGameyfinStatus').textContent = error.message; notify(error.message); }
    };
    $('importScummvmStore').onclick = async () => { try { const result = await api('/api/import/scummvm',{method:'POST',body:'{}'}); await refresh(); notify(`${result.added} ScummVM games imported`); } catch(error) { notify(error.message); } };
    $('importRpcs3Store').onclick = async () => { try { const result = await api('/api/import/rpcs3',{method:'POST',body:'{}'}); await refresh(); notify(`${result.added} RPCS3 games imported`); } catch(error) { notify(error.message); } };
    $('importVita3kStore').onclick = async () => { try { const result = await api('/api/import/vita3k',{method:'POST',body:'{}'}); await refresh(); notify(`${result.added} Vita3K games imported`); } catch(error) { notify(error.message); } };
    $('openThemeFolder').onclick = async () => { try { const result = await api('/api/themes/open-folder',{method:'POST',body:'{}'}); notify(`Opened ${result.path}`); } catch(error) { notify(error.message); } };
    $('injectRa').onclick = async () => { try { const result = await api('/api/ra/inject',{method:'POST',body:'{}'}); notify(`Updated ${result.updated.length} emulator config file${result.updated.length === 1 ? '' : 's'}`); } catch(error) { notify(error.message); } };
    $('cleanupMedia').onclick = async () => { try { const result = await api('/api/media/cleanup',{method:'POST',body:JSON.stringify({apply:false})}); AppState.duplicateMediaGroups = result.groups; $('applyCleanupMedia').hidden = !duplicateMediaGroups; notify(`${duplicateMediaGroups} duplicate media group${duplicateMediaGroups === 1 ? '' : 's'} found`); } catch(error) { notify(error.message); } };
    $('applyCleanupMedia').onclick = async () => { if (!confirm('Delete duplicate media files listed by the cleanup scan?')) return; try { const result = await api('/api/media/cleanup',{method:'POST',body:JSON.stringify({apply:true})}); $('applyCleanupMedia').hidden = true; notify(`Removed ${result.paths.length} duplicate file${result.paths.length === 1 ? '' : 's'}`); } catch(error) { notify(error.message); } };
    $('scanAllSaves').onclick = async () => { try { const result = await api('/api/saves/scan/apply',{method:'POST',body:'{}'}); await refresh(); notify(`Added save paths on ${result.updated} location${result.updated === 1 ? '' : 's'}`); } catch(error) { notify(error.message); } };
    $('bigBoxPause').onclick = event => { if (event.target === $('bigBoxPause')) $('bigBoxPause').hidden = true; };
    $('loadStorefrontCatalog').onclick = loadStorefrontCatalog;
    $('importStorefrontInstalled').onclick = () => importStorefrontCatalog(false);
    $('importStorefrontUninstalled').onclick = () => importStorefrontCatalog(true);
    document.querySelectorAll('[data-reader-layout]').forEach(button => button.onclick = () => {
      $('readerViewport').classList.toggle('spread', button.dataset.readerLayout === 'spread');
    });
    document.querySelectorAll('[data-reader-theme]').forEach(button => button.onclick = () => {
      $('readerFrame').style.filter = button.dataset.readerTheme === 'dark' ? 'invert(1) hue-rotate(180deg)' : '';
    });
    $('readerPrev').onclick = () => setReaderPage(readerPage - 1);
    $('readerNext').onclick = () => setReaderPage(readerPage + 1);
    $('addButton').onclick = () => openGameDialog(); $('importButton').onclick = importFolder; $('steamButton').onclick = importSteam; $('heroicButton').onclick = importHeroic; $('lutrisButton').onclick = importLutris; $('arcadeButton').onclick = importArcade; $('emulatorsButton').onclick = openProfiles; $('settingsButton').onclick = openSettings; $('bigBoxButton').onclick = openBigBox; $('sessionsButton').onclick = openSessions; $('historyButton').onclick = openHistory; $('themesButton').onclick = openThemes; $('saveFilterButton').onclick = saveFilter; $('savePresetButton').onclick = savePreset; $('playlistsButton').onclick = openPlaylists; $('achievementsButton').onclick = openAchievements; $('pluginsButton').onclick = openPlugins; $('mediaButton').onclick = openMediaManager; $('healthButton').onclick = health; $('bulkButton').onclick = bulkAction; $('backupButton').onclick = openBackups;
    $('closePlaylists').onclick = $('donePlaylists').onclick = () => $('playlistsDialog').close();
    $('newManualPlaylist').onclick = () => createManualPlaylist();
    $('newFilterPlaylist').onclick = createFilterPlaylist;
    $('closeBackups').onclick = $('doneBackups').onclick = () => $('backupDialog').close();
    $('refreshBackups').onclick = openBackups;
    $('createNamedBackup').onclick = createNamedBackup;
    $('contextPlay').onclick = () => { const id = contextGameId; closeContextMenu(); if (id !== null) launch(id); };
    $('contextFavorite').onclick = () => { const id = contextGameId; closeContextMenu(); if (id !== null) favorite(id); };
    $('contextEdit').onclick = () => { const game = games.find(item => item.id === contextGameId); closeContextMenu(); if (game) openGameDialog(game); };
    $('contextProgress').onclick = () => { const id = contextGameId; closeContextMenu(); if (id === null) return; const value = prompt('Progress value', games.find(game => game.id === id)?.progress || 'Playing'); if (value !== null) updateGameStatus(id, value); };
    $('contextAddPlaylist').onclick = () => { const name = $('contextPlaylist').value; const id = contextGameId; closeContextMenu(); if (name && id !== null) addGamesToPlaylist(name, [id]); };
    $('contextNewPlaylist').onclick = () => { const id = contextGameId; closeContextMenu(); if (id !== null) createManualPlaylist(id); };
    $('contextRemove').onclick = () => { const game = games.find(item => item.id === contextGameId); closeContextMenu(); if (game) removeGame(game.id, game.name); };
    document.addEventListener('contextmenu', event => {
      const target = event.target.closest?.('[data-game]');
      if (target) openContextMenu(event, Number(target.dataset.game));
    });
    document.addEventListener('click', event => {
      if (!event.target.closest?.('#contextMenu')) closeContextMenu();
      if (!event.target.closest?.('.topbar-tools')) document.querySelectorAll('.topbar-tools[open]').forEach(menu => menu.removeAttribute('open'));
    });
    if ($('scanEmulatorFolder')) $('scanEmulatorFolder').onclick = async () => {
      const folder = $('emulatorScanFolder').value.trim();
      if (!folder) return notify('Enter a ROM folder path first.');
      try {
        const result = await api('/api/emulators/scan',{method:'POST',body:JSON.stringify({folder})});
        await refresh();
        notify(`Added ${result.added} of ${result.found} scanned games`);
      } catch(error) { notify(error.message); }
    };
    $('fullscreenButton').onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
    $('surpriseButton').onclick = () => { const visible = filteredGames(); if (visible.length) { AppState.selectedId = visible[Math.floor(Math.random() * visible.length)].id; render(); } };
    $('imageGroup').onchange = async () => {
      const scope = activePlaylist ? 'playlist' : platform !== 'all' ? 'platform' : 'global';
      const name = activePlaylist || (platform === 'all' ? '' : platform);
      try {
        AppState.appSettings = await api('/api/image-group',{method:'POST',body:JSON.stringify({group:$('imageGroup').value,scope,name})});
        renderGrid();
      } catch(error) { notify(error.message); }
    };
    let lastDialogTrigger = null;
    const dialogTriggers = new WeakMap();
    document.addEventListener('click', event => {
      const trigger = event.target.closest?.('button,summary');
      if (trigger) lastDialogTrigger = trigger;
    }, true);
    const dialogObserver = new MutationObserver(() => {
      document.querySelectorAll('dialog[open]').forEach(dialog => {
        if (!dialogTriggers.has(dialog)) dialogTriggers.set(dialog, lastDialogTrigger);
      });
    });
    dialogObserver.observe(document.body, {subtree:true, attributes:true, attributeFilter:['open']});
    function openDialog(dialog, trigger = lastDialogTrigger || document.activeElement) {
      const opener = trigger instanceof HTMLElement ? trigger : null;
      if (opener) dialogTriggers.set(dialog, opener);
      dialog.showModal();
    }
    function closeDialog(dialog) {
      dialog.close();
    }
    $('closeDialog').onclick = $('cancelDialog').onclick = () => closeDialog($('gameDialog'));
    $('closeProfiles').onclick = $('cancelProfiles').onclick = () => closeDialog($('profilesDialog'));
    $('closeThemes').onclick = $('cancelThemes').onclick = () => closeDialog($('themesDialog'));
    $('closeAchievements').onclick = $('cancelAchievements').onclick = () => closeDialog($('achievementsDialog'));
    $('closePlugins').onclick = $('donePlugins').onclick = () => closeDialog($('pluginsDialog'));
    $('closeMetadata').onclick = $('doneMetadata').onclick = () => closeDialog($('metadataDialog'));
    $('closeMediaManager').onclick = $('doneMediaManager').onclick = () => closeDialog($('mediaManagerDialog'));
    $('closeHealth').onclick = $('doneHealth').onclick = () => closeDialog($('healthDialog'));
    $('closeBulk').onclick = $('cancelBulk').onclick = () => closeDialog($('bulkDialog'));
    $('closeSessions').onclick = $('doneSessions').onclick = () => closeDialog($('sessionsDialog'));
    $('closeHistory').onclick = $('doneHistory').onclick = () => closeDialog($('historyDialog'));
    $('closeWelcome').onclick = () => closeDialog($('welcomeDialog'));
    $('welcomeImportFolder').onclick = () => { $('welcomeDialog').close(); importFolder(); };
    $('welcomeImportSteam').onclick = () => { $('welcomeDialog').close(); importSteam(); };
    $('welcomeSyncMetadata').onclick = async () => {
      $('welcomeDialog').close();
      try {
        await api('/api/metadata/sync',{method:'POST',body:'{}'});
        notify('Metadata sync started');
        watchMetadata();
      } catch(error) { notify(error.message); }
    };
    $('welcomeOpenSettings').onclick = () => { $('welcomeDialog').close(); openSettings(); };
    $('welcomeDone').onclick = completeWelcome;
    $('reopenWelcome').onclick = () => $('welcomeDialog').showModal();
    $('settingsSearch').oninput = filterSettings;
    $('forceShutdown').onclick = () => gracefulShutdown(true);
    window.addEventListener('beforeunload', event => { if (AppState.runningGames.length) { event.preventDefault(); gracefulShutdown(); } });
    document.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key === ',') { event.preventDefault(); openSettings(); }
      if (event.key === 'Escape') {
        document.querySelectorAll('.topbar-tools[open]').forEach(menu => menu.removeAttribute('open'));
        closeContextMenu();
      }
    });
    $('closeSettings').onclick = $('cancelSettings').onclick = () => $('settingsDialog').close();
    $('closeBigBoxMenu').onclick = closeBigBoxMenu;
    $('applyBigBoxMenu').onclick = applyBigBoxMenu;
    $('screenSaver').onclick = stopScreenSaver;
    $('closeMedia').onclick = () => { $('mediaDialog').close(); $('fullScreenshot')?.remove(); };
    $('closeReader').onclick = () => { $('readerDialog').close(); $('readerFrame').removeAttribute('src'); AppState.readerUrl = ''; AppState.readerPage = 1; };
    $('bigBox').onkeydown = event => {
      if (!$('screenSaver').hidden) {
        const game = screenSaverGame;
        stopScreenSaver();
        if (event.key === 'Enter' && game) launch(game.id);
        event.preventDefault();
        return;
      }
      if (!$('bigBoxMenu').hidden) {
        if (event.key === 'Escape' || event.key === 'Backspace') closeBigBoxMenu();
        if (event.key === 'Enter') applyBigBoxMenu();
        return;
      }
      AppState.bigBoxLastInput = performance.now();
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') moveBigBox(-1);
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') moveBigBox(1);
      if (event.key === 'Enter') launch(bigBoxGames[bigBoxIndex].id);
      if (event.key.toLowerCase() === 'p' && runningGames.length) openBigBoxPause();
      if (event.key.toLowerCase() === 'm') openBigBoxMenu();
      if (event.key.toLowerCase() === 'r') { AppState.bigBoxIndex = Math.floor(Math.random() * bigBoxGames.length); renderBigBox(); }
      if (event.key.toLowerCase() === 'f') favoriteBigBox();
      if (event.key === 'Escape' || event.key === 'Backspace') closeBigBox();
    };
    document.addEventListener('mousedown', event => {
      // Native <select> menus paint outside the dialog rectangle; ignore those clicks.
      if (document.activeElement?.tagName === 'SELECT') return;
      if (event.target.closest?.('select, option')) return;
      document.querySelectorAll('dialog[open]').forEach(dialog => {
        if (dialog.contains(event.target)) return;
        const rect = dialog.getBoundingClientRect();
        const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
        if (!inside) dialog.close();
      });
      [
        [$('bigBoxMenu'), '.bigbox-menu-panel', closeBigBoxMenu],
        [$('bigBoxPause'), '.bigbox-pause-panel', () => { $('bigBoxPause').hidden = true; }],
      ].forEach(([overlay, panelSelector, close]) => {
        if (!overlay || overlay.hidden) return;
        if (event.target.closest(panelSelector)) return;
        if (overlay.contains(event.target)) close();
      });
    });
    document.querySelectorAll('dialog').forEach(dialog => {
      dialog.setAttribute('closedby', 'closerequest');
      const heading = dialog.querySelector('h2');
      if (heading) {
        if (!heading.id) heading.id = `${dialog.id}Title`;
        dialog.setAttribute('aria-labelledby', heading.id);
      }
      dialog.addEventListener('close', () => {
        if (dialog.id === 'readerDialog') {
          $('readerFrame').removeAttribute('src');
          AppState.readerUrl = '';
          AppState.readerPage = 1;
        }
        if (dialog.id === 'mediaDialog') $('fullScreenshot')?.remove();
        const trigger = dialogTriggers.get(dialog);
        dialogTriggers.delete(dialog);
        if (trigger?.isConnected) trigger.focus();
      });
    });
    const deeplink = new URLSearchParams(location.search);
    if (deeplink.get('deeplink') === 'search') $('sidebarSearch').value = deeplink.get('q') || '';
    if (deeplink.get('deeplink') === 'showgame') AppState.selectedId = Number(deeplink.get('id'));
    Promise.all([refresh(),loadTheme()]).then(async () => {
    let featureMode = '';
    async function openFeature(mode) {
      featureMode = mode;
      $('featureDialogTitle').textContent = mode === 'queue' ? 'Play Queue' : mode === 'tags' ? 'Tags' : mode === 'notifications' ? 'Notifications' : 'Webhook Settings';
      const content = $('featureContent');
      try {
        if (mode === 'queue') {
          const result = await api('/api/queue');
          content.innerHTML = `<div class="extras"><button class="primary" type="button" id="queueAdvance">Play next</button><button class="icon-button" type="button" id="queueAdd">Add selected game</button></div><div class="emulator-list">${(result.queue || []).map(item => `<div class="detail-card"><strong>${escapeHtml(item.name)}</strong><p class="description">${escapeHtml(item.platform || '')}${item.note ? ` · ${escapeHtml(item.note)}` : ''}</p><button type="button" class="icon-button" data-queue-remove="${escapeHtml(item.game_id)}">Remove</button></div>`).join('') || '<p class="description">Queue is empty.</p>'}</div>`;
          $('queueAdvance').onclick = async () => { try { await api('/api/queue',{method:'POST',body:JSON.stringify({action:'advance'})}); notify('Queue advanced'); openFeature('queue'); } catch (error) { notify(error.message); } };
          $('queueAdd').onclick = async () => { if (selectedId === null) return notify('Select a game first.'); const game = games[selectedId]; try { await api('/api/queue',{method:'POST',body:JSON.stringify({action:'enqueue',game_ids:[game.game_id]})}); notify('Game added to queue'); openFeature('queue'); } catch (error) { notify(error.message); } };
          document.querySelectorAll('[data-queue-remove]').forEach(button => button.onclick = async () => { await api('/api/queue',{method:'POST',body:JSON.stringify({action:'remove',game_ids:[button.dataset.queueRemove]})}); openFeature('queue'); });
        } else if (mode === 'tags') {
          const result = await api('/api/tags');
          content.innerHTML = `<p class="description">Select a game, then add or replace its tags.</p><div class="platforms">${(result.tags || []).map(item => `<button type="button" class="platform" data-tag-filter="${escapeHtml(item.tag)}">${escapeHtml(item.tag)} (${item.count})</button>`).join('') || '<span class="description">No tags yet.</span>'}</div><div class="extras"><input id="featureTagInput" placeholder="comma-separated tags"><button type="button" class="primary" id="saveFeatureTags">Save tags on selected game</button></div>`;
          document.querySelectorAll('[data-tag-filter]').forEach(button => button.onclick = () => { $('sidebarSearch').value = `tag:${button.dataset.tagFilter}`; $('featureDialog').close(); render(); });
          $('saveFeatureTags').onclick = async () => { if (selectedId === null) return notify('Select a game first.'); const tags = $('featureTagInput').value.split(',').map(value => value.trim()).filter(Boolean); try { await api('/api/tags',{method:'POST',body:JSON.stringify({ids:[games[selectedId].game_id],tags})}); await refresh(); openFeature('tags'); notify('Tags saved'); } catch (error) { notify(error.message); } };
        } else if (mode === 'notifications') {
          const result = await api('/api/notifications');
          content.innerHTML = `<div class="extras"><button type="button" class="primary" id="readNotifications">Mark all read</button><button type="button" class="icon-button" id="clearNotifications">Clear</button></div>${(result.notifications || []).map(item => `<div class="detail-card"><strong>${escapeHtml(item.title)}</strong><p class="description">${escapeHtml(item.body)}<br>${escapeHtml(item.created_at)}</p></div>`).join('') || '<p class="description">No notifications.</p>'}`;
          $('readNotifications').onclick = async () => { await api('/api/notifications',{method:'POST',body:JSON.stringify({action:'read'})}); openFeature('notifications'); };
          $('clearNotifications').onclick = async () => { await api('/api/notifications',{method:'POST',body:JSON.stringify({action:'clear'})}); openFeature('notifications'); };
        } else {
          const result = await api('/api/webhooks');
          content.innerHTML = `<p class="description">Webhook delivery uses signed JSON over HTTPS. HTTP requires OPENBOX_ALLOW_HTTP_WEBHOOKS=1.</p><label class="field wide"><span>URL</span><input id="webhookUrl" type="url"></label><label class="field wide"><span>Secret</span><input id="webhookSecret" type="password" autocomplete="off"></label><label class="field wide"><span>Events, comma separated</span><input id="webhookEvents" value="${escapeHtml(result.events.join(', '))}"></label><div class="extras"><button type="button" class="primary" id="saveWebhook">Save webhook</button><button type="button" class="icon-button" id="testWebhook">Test</button></div>${(result.webhooks || []).map(item => `<div class="detail-card"><strong>${escapeHtml(item.url)}</strong><p class="description">${escapeHtml(item.last_status || 'Not tested')} ${item.secret_set ? '· secret set' : ''}</p></div>`).join('')}`;
          $('saveWebhook').onclick = async () => { try { const current = result.webhooks[0]; await api('/api/webhooks',{method:'POST',body:JSON.stringify({webhooks:[{...(current || {}),url:$('webhookUrl').value.trim(),secret:$('webhookSecret').value,events:$('webhookEvents').value.split(',').map(value => value.trim()).filter(Boolean),enabled:true}]})}); notify('Webhook saved'); openFeature('webhooks'); } catch (error) { notify(error.message); } };
          $('testWebhook').onclick = async () => { try { const result = await api('/api/webhooks/test',{method:'POST',body:JSON.stringify({url:$('webhookUrl').value.trim(),secret:$('webhookSecret').value,events:['session.started']})}); notify(result.ok ? 'Webhook test succeeded' : result.error); } catch (error) { notify(error.message); } };
        }
        if (!$('featureDialog').open) $('featureDialog').showModal();
      } catch (error) { notify(error.message); }
    }
    $('queueButton').onclick = () => openFeature('queue');
    $('tagsButton').onclick = () => openFeature('tags');
    $('notificationsButton').onclick = () => openFeature('notifications');
    $('webhooksButton').onclick = () => openFeature('webhooks');
    $('closeFeature').onclick = $('doneFeature').onclick = () => $('featureDialog').close();
      pollSessions();
      maybeShowWelcome();
      await runStartupStorefrontImports().catch(() => {});
      if (deeplink.get('deeplink') === 'bigbox') openBigBox();
      else if (AppState.appSettings.gamescope_guest) openBigBox();
      if (deeplink.get('deeplink') === 'settings') openSettings();
      if (deeplink.get('deeplink') === 'showgame' && selectedId !== null) render();
    }).catch(error => notify(error.message));
