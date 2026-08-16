import { $, escapeHtml, duration, fact, RATIO_BUCKETS, RATIO_REP, coverBucketOf, artworkKinds } from './util.js';
import { AppState, selectedIds, media, renderBadges, platformCategoryFor, filteredGames, api, notify, applyLocaleStrings, applySidebarVisibility, loadExplorerFacets, token } from './state.js';
import { maybeShowWelcome, loadTheme, deletePlaylist } from './settings.js';
import { importFolder, importSteam, importHeroic, importLutris, importDroppedFolder } from './imports.js';
import { openGameDialog } from './dialogs.js';
import { openMetadata, steamMetadata, loadAchievements } from './metadata.js';
import { captureScreenshot, downloadBezel } from './media.js';
import { launch, backupSaves, discoverSaves, loadBackups } from './sessions.js';
import { installGameyfin, uninstallGameyfin, ludusaviAction, hoardAction } from './storefront.js';
import { openReader } from './reader.js';

let lastFacetsFingerprint = null;

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
      maybeShowWelcome();
      const fingerprint = `${AppState.games.length}:${AppState.games[0]?.id || ''}:${AppState.games.at(-1)?.id || ''}`;
      if (lastFacetsFingerprint !== fingerprint) {
        lastFacetsFingerprint = fingerprint;
        (typeof requestIdleCallback === 'function' ? requestIdleCallback : setTimeout)(() => loadExplorerFacets().catch(() => {}));
      }
    }
    function renderArtwork(game) {
      const items = artworkKinds.filter(([, , flag]) => game[flag]);
      return items.length ? `<div class="detail-card"><h3>Artwork</h3><div class="screenshot-grid">${items.map(([kind,label]) => kind === 'manual' ? `<button data-manual="${media(game,'manual')}" aria-label="Open ${escapeHtml(label)}"><div class="cover-title">${escapeHtml(label)}</div></button>` : `<button data-artwork="${kind}" aria-label="Open ${escapeHtml(label)}"><img src="${media(game,kind)}" alt="${escapeHtml(label)}" loading="lazy" decoding="async"></button>`).join('')}</div></div>` : '';
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
        const priority = game.id === AppState.selectedId ? 'high' : 'low';
        return `<img src="${media(game,imageGroup,index)}" alt="" loading="lazy" decoding="async" fetchpriority="${priority}" data-gid="${game.id}">`;
      }
      return `<div class="cover-title">${escapeHtml(game.name)}</div>`;
    }
    // Cover shape tracking: the load listener + render sweep record each cover's
    // natural aspect ratio (w/h) per game id. Unknown covers default to the
    // .cover-title fallback shape (portrait, aspect-ratio .72).
    let ratioRegroupTimer = null;
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
    function gridRowsGeometry(sections) {
      const [cellW, cols] = gridCellWidth();
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
      return {rows, cols, totalHeight: top};
    }
    function renderGroupedGrid(visible, imageGroup, fromScroll, motionClass) {
      const sections = groupedSections(visible);
      const {rows, cols, totalHeight} = gridRowsGeometry(sections);
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
      return {topSpacer:`<div class="grid-spacer" style="height:${firstTop}px"></div>`, bottomSpacer:`<div class="grid-spacer" style="height:${Math.max(0, totalHeight - lastBottom)}px"></div>`, rendered, geometry:{rows, cols, totalHeight}};
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
      const visible = filteredGames();
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
          : `<div class="empty"><div><h2>Start your library</h2><p>Bring your games into OpenBox, then search, filter, and launch them from one collection.</p><div class="empty-actions"><button id="emptyAdd">Add game</button><button class="empty-secondary" id="emptyImport">Import folder</button><button class="empty-secondary" id="emptySteam">Import Steam</button><button class="empty-secondary" id="emptyHeroic">Import Heroic</button><button class="empty-secondary" id="emptyLutris">Import Lutris</button></div></div></div>`;
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
        topSpacer = gridRowHeight ? `<div class="grid-spacer" style="height:${topHeight}px"></div>` : '';
        bottomSpacer = gridRowHeight ? `<div class="grid-spacer" style="height:${bottomHeight}px"></div>` : '';
        const chunk = visible.slice(start, end);
        rendered = chunk.map((game,index) => listView
           ? `<button type="button" class="list-row${motionClass} ${AppState.selectedId === game.id || selectedIds.has(game.id) ? 'selected' : ''}"${fromScroll ? '' : ` style="--motion-index:${Math.min(index,10)}"`} data-game="${game.id}" aria-label="Open ${escapeHtml(game.name)}"><strong>${escapeHtml(game.name)}<span class="badge-row">${renderBadges(game)}</span></strong><span>${escapeHtml(game.platform || '')}</span><span>${escapeHtml(game.genre || '')}</span><span>${escapeHtml(game.esrb || '-')}</span><span>${escapeHtml(game.progress || '')}</span><span>${game.play_count || 0}</span><span>${game.rating || ''}</span></button>`
           : gridCardHTML(game, index, imageGroup, fromScroll, motionClass)).join('');
      }
      $('grid').innerHTML = listView ? `<div class="list-head"><span>Title</span><span>Platform</span><span>Genre</span><span>ESRB</span><span>Progress</span><span>Plays</span><span>Rating</span></div>${topSpacer}${rendered}${bottomSpacer}` : `${topSpacer}${rendered}${bottomSpacer}`;
      document.querySelectorAll('#grid img[data-gid]').forEach(img => { if (img.complete) recordCoverRatio(img); });
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
      if (!measuredBefore && gridRowHeight && total) renderGrid({fromScroll});
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
          <div class="detail-actions"><button class="icon-button" id="favoriteButton">${game.favorite ? 'Remove favorite' : 'Add favorite'}</button><button class="icon-button" id="editButton">Edit metadata</button><button class="icon-button" id="databaseMetadataButton">Find metadata</button>${game.steam_app_id ? '<button class="icon-button" id="steamMetadataButton">Use Steam data</button>' : ''}<button class="icon-button" id="captureScreenshot">Capture screenshot</button><button class="icon-button" id="downloadBezel">Download bezel</button>${game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="uninstallGameyfin">Uninstall Gameyfin copy</button>' : ''}${game.path ? '<button class="icon-button" id="showInFolderButton">Show in folder</button>' : ''}<button class="icon-button" id="removeGameButton">Remove game</button></div>
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
        nativeOpenExternal(button.dataset.manual);
      });
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
    async function favorite(id) { try { const result = await api('/api/favorite',{method:'POST',body:JSON.stringify({id})}); const game = AppState.games.find(item => item.id === id); if (game) { game.favorite = result.favorite; AppState._refreshCounter = (AppState._refreshCounter || 0) + 1; } renderGrid(); renderDetails(); } catch(error) { notify(error.message); } }
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
    let searchTimer = null;
    $('sidebarSearch').oninput = () => { AppState.activePlaylist = ''; clearTimeout(searchTimer); searchTimer = setTimeout(() => { renderPlaylists(); renderGrid(); }, 150); };
    $('view').onchange = () => { AppState.activePlaylist = ''; renderPlaylists(); renderGrid(); };
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
    if ($('esrbFilter')) $('esrbFilter').onchange = renderGrid;
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
      window.addEventListener('resize', () => { gridRowHeight = 0; renderGrid(); });
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
        const names = [...event.dataTransfer.items].map(item => item.getAsFile?.()?.name).filter(Boolean);
        const hint = names.length ? `\n\nDropped: ${names.slice(0, 3).join(', ')}` : '';
        const folder = await nativePickFolder(`Enter the absolute path of the folder to import.${hint}`);
        if (folder) importDroppedFolder(folder.trim());
      });
      $('dropZone').onclick = () => importFolder();
    }

export { refresh, render, renderGrid, renderDetails, renderPlaylists, renderFilterPresets, renderPlatformCategories, renderPlatforms, selectGame, favorite, updateGameStatus, removeGame, launchExtra, loadRelated };
