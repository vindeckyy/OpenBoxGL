import { $, escapeHtml } from './util.js';
import { token, AppState, api, notify, nativeFullscreen, detectNative, filteredGames, nativePickFile, selectedIds, resetQuery, resolveDeeplinkGameId } from './state.js';
import { refresh, render, renderGrid, favorite, updateGameStatus, removeGame } from './library.js';
import { openSettings, openProfiles, openThemes, openAchievements, openPlugins, health, openBackups, openFeature, bulkAction, saveFilter, savePreset, openPlaylists, createManualPlaylist, createFilterPlaylist, createNamedBackup, filterSettings, gracefulShutdown, loadTheme, addGamesToPlaylist } from './settings.js';
import { importFolder, importSteam, importHeroic, importLutris, importArcade, runStartupStorefrontImports } from './imports.js';
import { watchMetadata } from './metadata.js';
import { openMediaManager } from './media.js';
import { setReaderPage } from './reader.js';
import { openSessions, openHistory, launch, connectSessionEvents, pollSessions } from './sessions.js';
import { openDiscovery, openStorefronts, saveStorefrontSettings, importStorefrontCatalog, loadStorefrontCatalog } from './storefront.js';
import { openBigBox, closeBigBox, openBigBoxMenu, closeBigBoxMenu, applyBigBoxMenu, moveBigBox, renderBigBox, stopScreenSaver, favoriteBigBox, openBigBoxPause, filteredBigBoxGames, applyLibraryMusic, activateCurrentGame, bigBoxTypingActive } from './bigbox.js';
import { openPicker } from './picker.js';
import { openConstellation } from './constellation.js';
import { openMastery } from './mastery.js';
import { closeDialog, openGameDialog, closeContextMenu, bindContextMenuA11y, promptChoice, promptInput, confirmAction } from './dialogs.js';
import { loadInsights, bindInsights } from './insights.js';
import { initNavigation } from './navigation.js';
import { applyHash } from './router.js';
import { initMood } from './mood.js';
import { init as i18nInit, setLocale, getSupportedLocales, t } from './i18n.js';
import './activity.js';

// Initialize i18n after the page has settled so it doesn't hold network
// connections during initial load (which would block networkidle2 in tests).
// Populate the locale selector from public_settings once settings are loaded.
function initI18n() {
  i18nInit().catch(() => {});
  // Populate the locale selector with available locales from AppState.
  try {
    const locales = (AppState.appSettings && AppState.appSettings.available_locales) ||
                    [{ code: 'en', name: 'English', native: 'English' }];
    const sel = $('localeSetting');
    if (sel) {
      sel.innerHTML = '';
      for (const loc of locales) {
        const opt = document.createElement('option');
        opt.value = loc.code;
        opt.textContent = loc.native || loc.name || loc.code;
        sel.appendChild(opt);
      }
      sel.value = (AppState.appSettings && AppState.appSettings.locale) || 'en';
      sel.onchange = () => { setLocale(sel.value).catch(() => {}); };
    }
  } catch { /* settings not ready yet — non-fatal */ }
}
window.addEventListener('DOMContentLoaded', () => setTimeout(initI18n, 0));
window.addEventListener('DOMContentLoaded', () => {
  bindInsights();
  document.addEventListener('app:show-game', event => {
    const gameId = String(event.detail?.gameId || '');
    if (!gameId) return;
    const game = AppState.games.find(item => item.game_id === gameId) ||
                 AppState.games.find(item => String(item.id) === gameId);
    if (!game) return;
    AppState.selectedId = game.id;
    render();
    document.querySelector(`[data-game="${game.id}"]`)?.scrollIntoView({ block: 'nearest' });
  });
});
// Scrub the token from browser history immediately after reading it: keep any
// deeplink params, drop only 'token'.
{
  const qs = new URLSearchParams(location.search);
  qs.delete('token');
  const q = qs.toString();
  history.replaceState(null, '', location.pathname + (q ? '?' + q : ''));
}

/** @type {any} */ (window).AppState = AppState;
/** @type {any} */ (window).filteredGames = filteredGames;

    const welcomeShim = $('welcomeDialog');
    if (welcomeShim) {
      welcomeShim.showModal = () => $('setupCenter').showModal();
      welcomeShim.close = () => $('setupCenter').close();
    }
    const openSetupCenter = () => $('setupCenter').showModal();
    if ($('setupLibraryButton')) $('setupLibraryButton').onclick = openSetupCenter;
    if ($('reopenWelcome')) $('reopenWelcome').onclick = openSetupCenter;
    if ($('closeSetupCenter')) $('closeSetupCenter').onclick = () => $('setupCenter').close();

    document.querySelectorAll('.game-editor-nav-item').forEach(button => {
      button.onclick = () => {
        document.querySelectorAll('.game-editor-nav-item').forEach(item => item.classList.toggle('active', item === button));
        const target = button.dataset.gameSection;
        document.querySelectorAll('.game-editor-section').forEach(panel => {
          panel.hidden = panel.dataset.gameSection !== target;
        });
      };
    });

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
      try { await api('/api/game',{method:'POST',body:JSON.stringify({id:AppState.editingId,game})}); $('gameDialog').close(); await refresh(); notify('Library saved'); } catch(error) { notify(error.message); }
    };
    $('bulkForm').onsubmit = async event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      const changes = {};
      for (const field of ['platform','genre']) if (values[field].trim()) changes[field] = values[field].trim();
      if (values.progress) changes.progress = values.progress === '__clear' ? '' : values.progress;
      if (values.rating !== '') changes.rating = Number(values.rating);
      for (const field of ['favorite','hidden']) if (values[field]) changes[field] = values[field] === 'true';
      if (values.esrb) changes.esrb = values.esrb;
      if (values.reset_stats) changes.reset_stats = true;
      try {
        const result = await api('/api/games/bulk-wizard',{method:'POST',body:JSON.stringify({ids:[...selectedIds],changes})});
        $('bulkDialog').close();
        selectedIds.clear();
        AppState.bulkMode = false;
        await refresh();
        notify(`${result.updated} games updated`);
      } catch(error) { notify(error.message); }
    };
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
        const pick = await promptChoice({
          title: 'Choose media pack',
          message: 'Select a bundled media pack to apply.',
          label: 'Media pack',
          defaultValue: '1',
          choices: packs.map((pack, index) => ({
            value: String(index + 1),
            label: `${index + 1}. ${pack.name}${pack.active ? ' (active)' : ''}`,
          })),
        });
        if (!pick) return;
        const pack = packs[Number(pick) - 1];
        if (!pack) return;
        const result = await api('/api/premium/media-packs/apply',{method:'POST',body:JSON.stringify({id:pack.id})});
        AppState.appSettings = result.settings || AppState.appSettings;
        $('mediaPackStatus').textContent = (AppState.appSettings.media_packs || []).filter(item => item.active).map(item => item.name).join(', ');
        notify(`Applied ${pack.name}`);
      } catch(error) { notify(error.message); }
    };
    $('libraryButton').onclick = () => { resetQuery(); AppState.selectedId = null; render(); loadTheme(); };
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
    $('cleanupMedia').onclick = async () => { try { const result = await api('/api/media/cleanup',{method:'POST',body:JSON.stringify({platform:AppState.platform,apply:false})}); AppState.duplicateMediaGroups = result.groups; $('applyCleanupMedia').hidden = !AppState.duplicateMediaGroups; notify(`${AppState.duplicateMediaGroups} duplicate media group${AppState.duplicateMediaGroups === 1 ? '' : 's'} found`); } catch(error) { notify(error.message); } };
    $('applyCleanupMedia').onclick = async () => {
      if (!await confirmAction({
        title: 'Delete duplicate media',
        target: 'Duplicate media files from the cleanup scan',
        consequence: 'Listed duplicate files will be deleted from disk.',
        retained: 'One copy of each media asset remains in your library.',
        recovery: 'Deleted files cannot be restored automatically.',
        confirmLabel: 'Delete duplicates',
        destructive: true,
      })) return;
      try {
        const result = await api('/api/media/cleanup',{method:'POST',body:JSON.stringify({platform:AppState.platform,apply:true})});
        $('applyCleanupMedia').hidden = true;
        notify(`Removed ${result.paths.length} duplicate file${result.paths.length === 1 ? '' : 's'}`);
      } catch(error) { notify(error.message); }
    };
    $('scanAllSaves').onclick = async () => { try { const result = await api('/api/saves/scan/apply',{method:'POST',body:'{}'}); await refresh(); notify(`Added save paths on ${result.updated} location${result.updated === 1 ? '' : 's'}`); } catch(error) { notify(error.message); } };
    $('bigBoxPause').onclick = event => { if (event.target === $('bigBoxPause')) $('bigBoxPause').hidden = true; };
    $('loadStorefrontCatalog').onclick = loadStorefrontCatalog;
    $('importStorefrontInstalled').onclick = () => importStorefrontCatalog(false);
    $('importStorefrontUninstalled').onclick = () => importStorefrontCatalog(true);
    $('addButton').onclick = () => openGameDialog(); $('importButton').onclick = importFolder; $('metadataButton').onclick = () => $('metadataDialog').showModal(); $('steamButton').onclick = importSteam; $('heroicButton').onclick = importHeroic; $('lutrisButton').onclick = importLutris; $('arcadeButton').onclick = importArcade; $('emulatorsButton').onclick = openProfiles; $('settingsButton').onclick = openSettings; $('bigBoxButton').onclick = openBigBox; $('sessionsButton').onclick = openSessions; $('historyButton').onclick = openHistory; $('themesButton').onclick = openThemes; $('saveFilterButton').onclick = saveFilter; $('savePresetButton').onclick = savePreset; $('playlistsButton').onclick = openPlaylists; $('achievementsButton').onclick = openAchievements; $('pluginsButton').onclick = openPlugins; $('mediaButton').onclick = openMediaManager; $('healthButton').onclick = health; $('constellationButton').onclick = openConstellation; $('masteryButton').onclick = openMastery; $('bulkButton').onclick = bulkAction; $('backupButton').onclick = openBackups;
    // ── Accessible Tools menu ──────────────────────────────────────────
    const toolsWrap = $('toolsWrap');
    const toolsButton = $('toolsButton');
    const toolMenu = $('toolMenu');
    const getToolItems = () => [...toolMenu.querySelectorAll('[role="menuitem"]:not(:disabled)')];
    const toolsMenuOpen = () => toolsWrap.classList.contains('open');
    function openToolsMenu() {
      toolsWrap.classList.add('open');
      toolsButton.setAttribute('aria-expanded', 'true');
      const first = getToolItems()[0];
      if (first) first.focus();
    }
    function closeToolsMenu(focusButton) {
      toolsWrap.classList.remove('open');
      toolsButton.setAttribute('aria-expanded', 'false');
      if (focusButton) toolsButton.focus();
    }
    toolsButton.addEventListener('click', () => { toolsMenuOpen() ? closeToolsMenu(false) : openToolsMenu(); });
    toolMenu.addEventListener('keydown', event => {
      const items = getToolItems();
      const idx = items.indexOf(document.activeElement);
      if (event.key === 'ArrowDown') { event.preventDefault(); items[(idx + 1) % items.length]?.focus(); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); items[(idx - 1 + items.length) % items.length]?.focus(); }
      else if (event.key === 'Home') { event.preventDefault(); items[0]?.focus(); }
      else if (event.key === 'End') { event.preventDefault(); items[items.length - 1]?.focus(); }
      else if (event.key === 'Escape') { event.preventDefault(); closeToolsMenu(true); }
      else if (event.key === 'Tab') { closeToolsMenu(false); }
    });
    toolMenu.addEventListener('click', event => { if (event.target.closest('[role="menuitem"]')) closeToolsMenu(false); });
    $('closePlaylists').onclick = $('donePlaylists').onclick = () => $('playlistsDialog').close();
    $('newManualPlaylist').onclick = () => createManualPlaylist();
    $('newFilterPlaylist').onclick = createFilterPlaylist;
    $('closeBackups').onclick = $('doneBackups').onclick = () => $('backupDialog').close();
    $('refreshBackups').onclick = openBackups;
    $('createNamedBackup').onclick = createNamedBackup;
    $('contextPlay').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id !== null) { const game = AppState.games.find(item => item.id === id); activateCurrentGame(game); } };
    $('contextFavorite').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id !== null) favorite(id); };
    $('contextEdit').onclick = () => { const game = AppState.games.find(item => item.id === AppState.contextGameId); closeContextMenu(); if (game) openGameDialog(game); };
    $('contextProgress').onclick = async () => {
      const id = AppState.contextGameId;
      closeContextMenu();
      if (id === null) return;
      const current = AppState.games.find(game => game.id === id)?.progress || 'Playing';
      const value = await promptInput({ title: 'Progress value', label: 'Progress', defaultValue: current });
      if (value !== null) updateGameStatus(id, value);
    };
    $('contextAddPlaylist').onclick = () => { const name = $('contextPlaylist').value; const id = AppState.contextGameId; closeContextMenu(); if (name && id !== null) addGamesToPlaylist(name, [id]); };
    $('contextNewPlaylist').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id !== null) createManualPlaylist(id); };
    $('contextResetStats').onclick = async () => {
      const id = AppState.contextGameId;
      closeContextMenu();
      if (id === null) return;
      const game = AppState.games.find(item => item.id === id);
      if (!game) return;
      if (!await confirmAction({
        title: 'Reset play statistics',
        target: game.name,
        consequence: 'Play count and last played date will be cleared.',
        retained: 'All other game metadata and media stay unchanged.',
        recovery: 'Play statistics cannot be restored after reset.',
        confirmLabel: 'Reset statistics',
        destructive: true,
      })) return;
      try {
        await api('/api/games/bulk-wizard', {method:'POST',body:JSON.stringify({ids:[id],changes:{reset_stats:true}})});
        await refresh();
        notify(`Reset play statistics for ${game.name}`);
      } catch(error) { notify(error.message); }
    };
    $('contextRemove').onclick = () => { const game = AppState.games.find(item => item.id === AppState.contextGameId); closeContextMenu(); if (game) removeGame(game.id, game.name); };
    bindContextMenuA11y();
    document.addEventListener('click', event => {
      if (toolsMenuOpen() && !toolsWrap.contains(event.target)) closeToolsMenu(false);
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
    $('fullscreenButton').onclick = () => nativeFullscreen().catch(() => {});
    $('surpriseButton').onclick = openPicker;
    $('imageGroup').onchange = async () => {
      const scope = AppState.activePlaylist ? 'playlist' : AppState.platform !== 'all' ? 'platform' : 'global';
      const name = AppState.activePlaylist || (AppState.platform === 'all' ? '' : AppState.platform);
      try {
        AppState.appSettings = await api('/api/image-group',{method:'POST',body:JSON.stringify({group:$('imageGroup').value,scope,name})});
        renderGrid();
      } catch(error) { notify(error.message); }
    };

    $('settingsSearch').oninput = filterSettings;
    $('forceShutdown').onclick = () => gracefulShutdown(true);
    window.addEventListener('beforeunload', event => { if (AppState.runningGames.length) { event.preventDefault(); gracefulShutdown(); } });
    document.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key === ',') { event.preventDefault(); openSettings(); }
      if ((event.ctrlKey || event.metaKey) && event.altKey && (event.key.toLowerCase() === 'q' || event.key.toLowerCase() === 'r')) {
        event.preventDefault();
        const visible = filteredGames();
        if (visible.length) {
          AppState.selectedId = visible[Math.floor(Math.random() * visible.length)].id;
          render();
          const card = document.querySelector(`[data-game="${AppState.selectedId}"]`);
          if (card) card.scrollIntoView({block:'nearest'});
        }
      }
      if (event.key === 'F11') { event.preventDefault(); nativeFullscreen().catch(() => {}); }
      if (event.key === 'Escape') {
        if (toolsMenuOpen()) closeToolsMenu(true);
      }
    });
    $('closeSettings').onclick = $('cancelSettings').onclick = () => $('settingsDialog').close();
    $('closeBigBoxMenu').onclick = closeBigBoxMenu;
    $('applyBigBoxMenu').onclick = applyBigBoxMenu;
    $('screenSaver').onclick = stopScreenSaver;
    $('closeMedia').onclick = () => { $('mediaDialog').close(); $('mediaDialog').querySelectorAll('img').forEach(el => el.remove()); };
    $('closeReader').onclick = () => { $('readerDialog').close(); $('readerFrame').removeAttribute('src'); AppState.readerUrl = ''; AppState.readerPage = 1; };
    $('bigBox').onkeydown = event => {
      if (bigBoxTypingActive()) {
        if (event.key === 'Escape') $('bigBoxHybridSearch').blur();
        return;
      }
      if (!$('screenSaver').hidden) {
        const game = AppState.screenSaverGame;
        stopScreenSaver();
        if (event.key === 'Enter' && game) activateCurrentGame(game);
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
      if (event.key === 'Enter') {
        const target = AppState.bigBoxGames[AppState.bigBoxIndex];
        if (target) activateCurrentGame(target);
      }
      if (event.key.toLowerCase() === 'p' && AppState.runningGames.length) openBigBoxPause();
      if (event.key.toLowerCase() === 'm') openBigBoxMenu();
      if (event.key.toLowerCase() === 'r') { AppState.bigBoxIndex = Math.floor(Math.random() * AppState.bigBoxGames.length); renderBigBox(); }
      if (event.key.toLowerCase() === 'f') favoriteBigBox();
      if (event.key === 'Escape' || event.key === 'Backspace') closeBigBox();
    };

    const deeplink = new URLSearchParams(location.search);
    if (deeplink.get('deeplink') === 'search') $('sidebarSearch').value = deeplink.get('q') || '';
    // Restore the view context from the URL hash before the first render.
    applyHash();
    window.addEventListener('hashchange', () => { if (applyHash()) render(); });
    initNavigation();
    Promise.all([refresh(),loadTheme()]).then(async () => {
      initMood();
    $('queueButton').onclick = () => openFeature('queue');
    $('tagsButton').onclick = () => openFeature('tags');
    $('notificationsButton').onclick = () => openFeature('notifications');
    $('webhooksButton').onclick = () => openFeature('webhooks');
    $('closeFeature').onclick = $('doneFeature').onclick = () => $('featureDialog').close();
      await detectNative().catch(() => {});
      connectSessionEvents();
      pollSessions();
      await runStartupStorefrontImports().catch(() => {});
      if (deeplink.get('deeplink') === 'bigbox') openBigBox();
      else if (AppState.appSettings.gamescope_guest) openBigBox();
      if (deeplink.get('deeplink') === 'settings') openSettings();
      if (deeplink.get('deeplink') === 'showgame') {
        const resolved = resolveDeeplinkGameId(deeplink.get('id'));
        if (resolved !== null) {
          AppState.selectedId = resolved;
          render();
        }
      }
    }).catch(error => notify(error.message));
