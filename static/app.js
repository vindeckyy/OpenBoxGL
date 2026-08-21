import { $, escapeHtml } from './util.js';
import { token, AppState, api, notify, nativeFullscreen, detectNative, filteredGames, nativePickFile, selectedIds } from './state.js';
import { refresh, render, renderGrid, favorite, updateGameStatus, removeGame } from './library.js';
import { openSettings, openProfiles, openThemes, openAchievements, openPlugins, health, openBackups, openFeature, bulkAction, saveFilter, savePreset, openPlaylists, createManualPlaylist, createFilterPlaylist, createNamedBackup, completeWelcome, filterSettings, gracefulShutdown, loadTheme } from './settings.js';
import { importFolder, importSteam, importHeroic, importLutris, importArcade, runStartupStorefrontImports } from './imports.js';
import { watchMetadata } from './metadata.js';
import { openMediaManager } from './media.js';
import { setReaderPage } from './reader.js';
import { openSessions, openHistory, launch, connectSessionEvents, pollSessions } from './sessions.js';
import { openDiscovery, openStorefronts, saveStorefrontSettings, importStorefrontCatalog, loadStorefrontCatalog } from './storefront.js';
import { openBigBox, closeBigBox, openBigBoxMenu, closeBigBoxMenu, applyBigBoxMenu, moveBigBox, renderBigBox, stopScreenSaver, favoriteBigBox, openBigBoxPause, filteredBigBoxGames, applyLibraryMusic } from './bigbox.js';
import { closeDialog, openGameDialog, openContextMenu, closeContextMenu } from './dialogs.js';
// Scrub the token from browser history immediately after reading it: keep any
// deeplink params, drop only 'token'.
{
  const qs = new URLSearchParams(location.search);
  qs.delete('token');
  const q = qs.toString();
  history.replaceState(null, '', location.pathname + (q ? '?' + q : ''));
}

window.AppState = AppState;
window.filteredGames = filteredGames;

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
        const labels = packs.map((pack,index) => `${index + 1}. ${pack.name}${pack.active ? ' (active)' : ''}`).join('\n');
        const pick = prompt(`Choose a bundled media pack:\n${labels}`, '1');
        const pack = packs[Number(pick) - 1];
        if (!pack) return;
        const result = await api('/api/premium/media-packs/apply',{method:'POST',body:JSON.stringify({id:pack.id})});
        AppState.appSettings = result.settings || AppState.appSettings;
        $('mediaPackStatus').textContent = (AppState.appSettings.media_packs || []).filter(item => item.active).map(item => item.name).join(', ');
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
    $('cleanupMedia').onclick = async () => { try { const result = await api('/api/media/cleanup',{method:'POST',body:JSON.stringify({platform:AppState.platform,apply:false})}); AppState.duplicateMediaGroups = result.groups; $('applyCleanupMedia').hidden = !AppState.duplicateMediaGroups; notify(`${AppState.duplicateMediaGroups} duplicate media group${AppState.duplicateMediaGroups === 1 ? '' : 's'} found`); } catch(error) { notify(error.message); } };
    $('applyCleanupMedia').onclick = async () => { if (!confirm('Delete duplicate media files listed by the cleanup scan?')) return; try { const result = await api('/api/media/cleanup',{method:'POST',body:JSON.stringify({platform:AppState.platform,apply:true})}); $('applyCleanupMedia').hidden = true; notify(`Removed ${result.paths.length} duplicate file${result.paths.length === 1 ? '' : 's'}`); } catch(error) { notify(error.message); } };
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
    $('addButton').onclick = () => openGameDialog(); $('importButton').onclick = importFolder; $('steamButton').onclick = importSteam; $('heroicButton').onclick = importHeroic; $('lutrisButton').onclick = importLutris; $('arcadeButton').onclick = importArcade; $('emulatorsButton').onclick = openProfiles; $('settingsButton').onclick = openSettings; $('bigBoxButton').onclick = openBigBox; $('sessionsButton').onclick = openSessions; $('historyButton').onclick = openHistory; $('themesButton').onclick = openThemes; $('saveFilterButton').onclick = saveFilter; $('savePresetButton').onclick = savePreset; $('playlistsButton').onclick = openPlaylists; $('achievementsButton').onclick = openAchievements; $('pluginsButton').onclick = openPlugins; $('mediaButton').onclick = openMediaManager; $('healthButton').onclick = health; $('bulkButton').onclick = bulkAction; $('backupButton').onclick = openBackups;
    $('closePlaylists').onclick = $('donePlaylists').onclick = () => $('playlistsDialog').close();
    $('newManualPlaylist').onclick = () => createManualPlaylist();
    $('newFilterPlaylist').onclick = createFilterPlaylist;
    $('closeBackups').onclick = $('doneBackups').onclick = () => $('backupDialog').close();
    $('refreshBackups').onclick = openBackups;
    $('createNamedBackup').onclick = createNamedBackup;
    $('contextPlay').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id !== null) launch(id); };
    $('contextFavorite').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id !== null) favorite(id); };
    $('contextEdit').onclick = () => { const game = AppState.games.find(item => item.id === AppState.contextGameId); closeContextMenu(); if (game) openGameDialog(game); };
    $('contextProgress').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id === null) return; const value = prompt('Progress value', AppState.games.find(game => game.id === id)?.progress || 'Playing'); if (value !== null) updateGameStatus(id, value); };
    $('contextAddPlaylist').onclick = () => { const name = $('contextPlaylist').value; const id = AppState.contextGameId; closeContextMenu(); if (name && id !== null) addGamesToPlaylist(name, [id]); };
    $('contextNewPlaylist').onclick = () => { const id = AppState.contextGameId; closeContextMenu(); if (id !== null) createManualPlaylist(id); };
    $('contextResetStats').onclick = async () => {
      const id = AppState.contextGameId;
      closeContextMenu();
      if (id === null) return;
      const game = AppState.games.find(item => item.id === id);
      if (!game) return;
      if (!confirm(`Reset play count and last played date for "${game.name}"?`)) return;
      try {
        await api('/api/games/bulk-wizard', {method:'POST',body:JSON.stringify({ids:[id],changes:{reset_stats:true}})});
        await refresh();
        notify(`Reset play statistics for ${game.name}`);
      } catch(error) { notify(error.message); }
    };
    $('contextRemove').onclick = () => { const game = AppState.games.find(item => item.id === AppState.contextGameId); closeContextMenu(); if (game) removeGame(game.id, game.name); };
    document.addEventListener('contextmenu', event => {
      const target = event.target.closest?.('[data-game]');
      if (target) openContextMenu(event, Number(target.dataset.game));
    });
    document.addEventListener('click', event => {
      if (!event.target.closest?.('#contextMenu')) closeContextMenu();
      const tools = event.target.closest?.('.topbar-tools');
      const closeToolsMenus = () => document.querySelectorAll('.topbar-tools[open]').forEach(menu => menu.removeAttribute('open'));
      if (!tools) { closeToolsMenus(); return; }
      if (event.target.closest('.tool-menu button')) {
        closeToolsMenus();
      }
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
    $('surpriseButton').onclick = () => { const visible = filteredGames(); if (visible.length) { AppState.selectedId = visible[Math.floor(Math.random() * visible.length)].id; render(); } };
    $('imageGroup').onchange = async () => {
      const scope = AppState.activePlaylist ? 'playlist' : AppState.platform !== 'all' ? 'platform' : 'global';
      const name = AppState.activePlaylist || (AppState.platform === 'all' ? '' : AppState.platform);
      try {
        AppState.appSettings = await api('/api/image-group',{method:'POST',body:JSON.stringify({group:$('imageGroup').value,scope,name})});
        renderGrid();
      } catch(error) { notify(error.message); }
    };
    $('closeWelcome').onclick = () => closeDialog($('welcomeDialog'));
    $('welcomeImportFolder').onclick = () => { $('welcomeDialog').close(); importFolder(); };
    $('welcomeImportSteam').onclick = () => { $('welcomeDialog').close(); importSteam(); };
    $('welcomeImportHeroic').onclick = () => { $('welcomeDialog').close(); importHeroic(); };
    $('welcomeImportLutris').onclick = () => { $('welcomeDialog').close(); importLutris(); };
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
        document.querySelectorAll('.topbar-tools[open]').forEach(menu => menu.removeAttribute('open'));
        closeContextMenu();
      }
    });
    $('closeSettings').onclick = $('cancelSettings').onclick = () => $('settingsDialog').close();
    $('closeBigBoxMenu').onclick = closeBigBoxMenu;
    $('applyBigBoxMenu').onclick = applyBigBoxMenu;
    $('screenSaver').onclick = stopScreenSaver;
    $('closeMedia').onclick = () => { $('mediaDialog').close(); $('mediaDialog').querySelectorAll('img').forEach(el => el.remove()); };
    $('closeReader').onclick = () => { $('readerDialog').close(); $('readerFrame').removeAttribute('src'); AppState.readerUrl = ''; AppState.readerPage = 1; };
    $('bigBox').onkeydown = event => {
      if (!$('screenSaver').hidden) {
        const game = AppState.screenSaverGame;
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
      if (event.key === 'Enter') {
        const target = AppState.bigBoxGames[AppState.bigBoxIndex];
        if (target) launch(target.id);
      }
      if (event.key.toLowerCase() === 'p' && AppState.runningGames.length) openBigBoxPause();
      if (event.key.toLowerCase() === 'm') openBigBoxMenu();
      if (event.key.toLowerCase() === 'r') { AppState.bigBoxIndex = Math.floor(Math.random() * AppState.bigBoxGames.length); renderBigBox(); }
      if (event.key.toLowerCase() === 'f') favoriteBigBox();
      if (event.key === 'Escape' || event.key === 'Backspace') closeBigBox();
    };

    const deeplink = new URLSearchParams(location.search);
    if (deeplink.get('deeplink') === 'search') $('sidebarSearch').value = deeplink.get('q') || '';
    if (deeplink.get('deeplink') === 'showgame') AppState.selectedId = Number(deeplink.get('id'));
    Promise.all([refresh(),loadTheme()]).then(async () => {

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
      if (deeplink.get('deeplink') === 'showgame' && AppState.selectedId !== null) render();
    }).catch(error => notify(error.message));
