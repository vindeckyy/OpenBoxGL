import { $, escapeHtml, applyFontFamily } from './util.js';
import { matchesKey } from './pure.js';
import { token, AppState, api, notify, showErrorBanner, nativeCaps, nativeFullscreen, nativeOpenExternal, detectNative, filteredGames, nativePickFile, selectedIds, resetQuery, resolveDeeplinkGameId } from './state.js';
import { refresh, render, renderGrid, favorite, updateGameStatus, removeGame, focusSelectedGame } from './library.js';
import { setupDetailsPopup } from './details.js';
import { openSettings, openProfiles, openThemes, openAchievements, openPlugins, health, openBackups, openFeature, bulkAction, saveFilter, savePreset, openPlaylists, createManualPlaylist, createFilterPlaylist, createNamedBackup, filterSettings, gracefulShutdown, loadTheme, addGamesToPlaylist } from './settings.js';
import { importFolder, importSteam, importHeroic, importEpic, importLutris, importGog, importEa, importUbisoft, importArcade, importPico8, importMsu1, importWhdload, runStartupStorefrontImports, bindLaunchBoxMigration, bindEsdeImport } from './imports.js';
import { watchMetadata } from './metadata.js';
import { openMediaManager } from './media.js';
import { resetReaderFrame } from './reader.js';
import { openSessions, openHistory, launch, connectSessionEvents, pollSessions } from './sessions.js';
import { openSessionRecap } from './recap.js';
import { openDiscovery, openStorefronts, saveStorefrontSettings, importStorefrontCatalog, loadStorefrontCatalog, scanStorefronts } from './storefront.js';
import { openBigBox, closeBigBox, openBigBoxMenu, closeBigBoxMenu, applyBigBoxMenu, moveBigBox, renderBigBox, stopScreenSaver, favoriteBigBox, openBigBoxPause, closeBigBoxPause, filteredBigBoxGames, applyLibraryMusic, activateCurrentGame, bigBoxTypingActive, startBigBoxBack, releaseBigBoxBack } from './bigbox.js';
import { openPicker } from './picker.js';
import { openConstellation } from './constellation.js';
import { openMastery } from './mastery.js';
import { closeDialog, openGameDialog, closeContextMenu, bindContextMenuA11y, promptChoice, promptInput, confirmAction } from './dialogs.js';
import { loadInsights, bindInsights } from './insights.js';
import { openTimeMachine } from './timemachine.js';
import { openMoment } from './moments.js';
import { openClip } from './clips.js';
import { openArcadeRoom } from './arcaderoom.js';
import { initHousehold, openHousehold } from './household.js';
import { initNavigation } from './navigation.js';
import { initPalette } from './palette.js';
import { initWhatsNew } from './whatsnew.js';
import { applyHash } from './router.js';
import { initMood } from './mood.js';
import { dnaMoreLikeThis } from './dna.js';
import { openSetupCenter, markSetupDismissed } from './setup.js';
import { openHelp } from './help.js';
import { init as i18nInit, setLocale, getSupportedLocales, t } from './i18n.js';
import './activity.js';
import './netplay.js';

// Expose the app state and game filter to the page global scope for the
// ui-smoke harness (page.evaluate) and other page-level consumers.
window.AppState = AppState;
window.filteredGames = filteredGames;

const KEYBOARD_DEFAULTS = {
  play: 'Enter', back: 'Escape', favorite: 'f', random: 'r',
  page_left: 'PageUp', page_right: 'PageDown', pause: 'p', menu: 'm',
  search: '/', settings: 'Ctrl+,', reader_prev: 'ArrowLeft', reader_next: 'ArrowRight',
};
function keyAction(action) {
  return { ...KEYBOARD_DEFAULTS, ...(AppState.appSettings.keyboard_map || {}) }[action];
}
function isBackKey(event) {
  if (matchesKey(event, keyAction('back'))) return true;
  // Backspace used to be a second default back key. Keep it, but only while
  // Back is still the default: a remapped Back key must not inherit it.
  return keyAction('back') === 'Escape' && event.key === 'Backspace' && !event.ctrlKey && !event.altKey && !event.metaKey;
}

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

async function verifyMuseumPin() {
  const pin = await promptInput({ title: 'Kiosk PIN', label: 'PIN', defaultValue: '' });
  if (pin === null) return false;
  try {
    const result = await api('/api/v2/arcade/kiosk/verify', {
      method: 'POST',
      body: JSON.stringify({pin}),
    });
    if (!result.ok) notify(t('notify.app.museum_pin_rejected'));
    return Boolean(result.ok);
  } catch (error) {
    notify(error.message);
    return false;
  }
}
window.addEventListener('DOMContentLoaded', () => setTimeout(initI18n, 0));
window.addEventListener('DOMContentLoaded', () => initPalette());
window.addEventListener('DOMContentLoaded', () => initHousehold());
window.addEventListener('DOMContentLoaded', () => initWhatsNew());
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
  document.addEventListener('app:palette-open-settings', () => $('settingsButton')?.click());
  document.addEventListener('app:palette-open-time-machine', () => $('timeMachineButton')?.click());
  document.addEventListener('app:palette-open-bigbox', () => $('bigBoxButton')?.click());
  document.addEventListener('app:palette-open-arcade-room', () => $('arcadeRoomButton')?.click());
  document.addEventListener('app:palette-open-household', () => $('householdButton')?.click());
  document.addEventListener('app:palette-surprise', () => $('surpriseButton')?.click());
  document.addEventListener('app:palette-focus-search', () => $('sidebarSearch')?.focus());
});
// Scrub the token from browser history immediately after reading it: keep any
// deeplink params, drop only 'token'.
{
  const qs = new URLSearchParams(location.search);
  qs.delete('token');
  const q = qs.toString();
  if (token) sessionStorage.setItem('openbox.token', token);
  history.replaceState(null, '', location.pathname + (q ? '?' + q : ''));
}

/** @type {any} */ (window).AppState = AppState;
/** @type {any} */ (window).filteredGames = filteredGames;

    const welcomeShim = $('welcomeDialog');
    if (welcomeShim) {
      // The legacy welcomeDialog is a shim over the setup wizard: route
      // through the single open entry point so it never shows stale content.
      welcomeShim.showModal = () => openSetupCenter();
      welcomeShim.close = () => { markSetupDismissed(); $('setupCenter').close(); };
    }
    if ($('setupLibraryButton')) $('setupLibraryButton').onclick = () => openSetupCenter();
    if ($('reopenWelcome')) $('reopenWelcome').onclick = () => openSetupCenter();

    document.querySelectorAll('.game-editor-nav-item').forEach(button => {
      button.onclick = () => {
        document.querySelectorAll('.game-editor-nav-item').forEach(item => item.classList.toggle('active', item === button));
        const target = button.dataset.gameSection;
        document.querySelectorAll('.game-editor-section').forEach(panel => {
          panel.hidden = panel.dataset.gameSection !== target;
        });
      };
    });
    // A required field on a hidden tab would fail validation silently — jump to it.
    $('gameForm').addEventListener('invalid', event => {
      const section = event.target.closest?.('.game-editor-section');
      if (!section?.hidden) return;
      const target = section.dataset.gameSection;
      document.querySelectorAll('.game-editor-nav-item').forEach(item => item.classList.toggle('active', item.dataset.gameSection === target));
      document.querySelectorAll('.game-editor-section').forEach(panel => { panel.hidden = panel.dataset.gameSection !== target; });
    }, true);

    $('gameForm').onsubmit = async event => {
      event.preventDefault();
      const game = Object.fromEntries(new FormData(event.currentTarget));
      const shelf = event.currentTarget.elements.entry_type?.value === 'shelf';
      delete game.entry_type;
      if (game.price_paid !== '') {
        const cents = Math.round(Number(game.price_paid) * 100);
        const currency = String(game.price_currency || AppState.appSettings.currency || 'USD').trim().toUpperCase();
        if (!Number.isFinite(cents) || cents < 0) { notify(t('price.invalid')); return; }
        if (!/^[A-Z]{3}$/.test(currency)) { notify(t('price.currency_invalid')); return; }
        game.price_paid_cents = cents;
        game.price_currency = currency;
      } else {
        delete game.price_currency;
      }
      delete game.price_paid;
      game.extract_archive = event.currentTarget.elements.extract_archive.checked;
      game.launch_confirm = event.currentTarget.elements.launch_confirm.checked;
      game.hidden = event.currentTarget.elements.hidden.checked;
      game.hide_in_bigbox = event.currentTarget.elements.hide_in_bigbox.checked;
      game.alternate_names = String(game.alternate_names || '').split(';').map(value => value.trim()).filter(Boolean);
      game.applications = String(game.applications || '').split('\n').filter(Boolean).map(line => { const [name,path,command=''] = line.split('|').map(value => value.trim()); return {name,path,command}; });
      game.versions = String(game.versions || '').split('\n').filter(Boolean).map(line => { const [name,path,command=''] = line.split('|').map(value => value.trim()); return {name,path,command}; });
      game.documents = String(game.documents || '').split('\n').filter(Boolean).map(line => { const [name,path] = line.split('|').map(value => value.trim()); return {name,path}; });
      game.save_paths = String(game.save_paths || '').split('\n').map(value => value.trim()).filter(Boolean);
      game.screenshots = String(game.screenshots || '').split('\n').map(value => value.trim()).filter(Boolean);
      game.custom_fields = Object.fromEntries([...document.querySelectorAll('[data-custom-field]')].map(input => [input.dataset.customField, input.value.trim()]).filter(([, value]) => value));
      game.launch_env = Object.fromEntries(String(game.launch_env || '').split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#')).map(line => { const i = line.indexOf('='); return [i < 0 ? line : line.slice(0, i).trim(), i < 0 ? '' : line.slice(i + 1).trim()]; }));
      if (shelf) {
        game.manual_entry = true;
        game.path = '';
      } else delete game.manual_entry;
      try {
        const endpoint = shelf && AppState.editingId !== null
          ? '/api/v2/library/manual-entry/update'
          : shelf ? '/api/v2/library/manual-entry' : '/api/game';
        await api(endpoint,{method:'POST',body:JSON.stringify({id:AppState.editingId,...(AppState.editingGameId ? {game_id:AppState.editingGameId} : {}),game})});
        $('gameDialog').close();
        await refresh();
        notify(shelf ? 'Shelf entry saved' : 'Library saved');
      } catch(error) { notify(error.message); }
    };
    $('bulkForm').onsubmit = async event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      const changes = {};
      for (const field of ['platform','genre']) if (values[field].trim()) changes[field] = values[field].trim();
      if (values.progress) changes.progress = values.progress === '__clear' ? '' : values.progress;
      if (values.rating !== '') changes.rating = Number(values.rating);
      if (values.user_rating !== '') changes.user_rating = Number(values.user_rating);
      for (const field of ['favorite','hidden']) if (values[field]) changes[field] = values[field] === 'true';
      if (values.esrb) changes.esrb = values.esrb;
      if (values.price_clear) {
        changes.price_paid_cents = null;
      } else if (values.price_paid !== '') {
        const cents = Math.round(Number(values.price_paid) * 100);
        const currency = String(values.price_currency || AppState.appSettings.currency || 'USD').trim().toUpperCase();
        if (!Number.isFinite(cents) || cents < 0 || !/^[A-Z]{3}$/.test(currency)) {
          notify(t('price.invalid'));
          return;
        }
        changes.price_paid_cents = cents;
        changes.price_currency = currency;
      }
      if (values.reset_stats) changes.reset_stats = true;
      const findText = String(values.find_text || '');
      if (findText) {
        const ids = [...selectedIds];
        try {
          const preview = await api('/api/v2/bulk/find-replace', { method: 'POST', body: JSON.stringify({ ids, field: values.find_field, find: findText, replace: values.replace_text || '', dry_run: true }) });
          if (!preview.matched) { notify(t('notify.app.bulk_no_matches')); return; }
          const sample = preview.games.slice(0, 5).map(game => `${game.before} → ${game.after}`).join('\n');
          const ok = await confirmAction({ title: `Replace in ${preview.matched} game${preview.matched === 1 ? '' : 's'}`, message: `${sample}${preview.truncated ? `\n…and ${preview.truncated} more` : ''}`, consequence: 'Only the selected field changes.', confirmLabel: 'Replace' });
          if (!ok) return;
          await api('/api/v2/bulk/find-replace', { method: 'POST', body: JSON.stringify({ ids, field: values.find_field, find: findText, replace: values.replace_text || '', dry_run: false }) });
        } catch (error) { notify(error.message); return; }
      }
      try {
        const result = await api('/api/games/bulk-wizard',{method:'POST',body:JSON.stringify({ids:[...selectedIds],changes,migrate_media:Boolean(values.migrate_media) && Boolean(changes.platform)})});
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
        if (!packs.length) return notify(t('notify.app.no_media_packs'));
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
        notify(t('notify.app.pack_applied', {name: pack.name}));
      } catch(error) { notify(error.message); }
    };
    $('libraryButton').onclick = () => { resetQuery(); AppState.selectedId = null; render(); loadTheme(); };
    $('reportIssueButton').onclick = async () => {
      const target = 'https://github.com/vindeckyy/OpenBoxGL/issues/new/choose';
      try {
        if (await nativeOpenExternal(target)) return;
      } catch { /* fall through to the browser */ }
      window.open(target, '_blank', 'noopener,noreferrer');
    };
    $('discoveryButton').onclick = openDiscovery;
    $('storefrontButton').onclick = openStorefronts;
    $('closeDiscovery').onclick = $('doneDiscovery').onclick = () => $('discoveryDialog').close();
    $('closeStorefront').onclick = () => $('storefrontDialog').close();
    $('scanStorefronts').onclick = scanStorefronts;
    $('saveStorefront').onclick = saveStorefrontSettings;
    $('importSteamStore').onclick = () => { $('storefrontDialog').close(); importSteam(); };
    $('importHeroicStore').onclick = () => { $('storefrontDialog').close(); importHeroic(); };
    $('importEpicStore').onclick = () => { $('storefrontDialog').close(); importEpic(); };
    $('importLutrisStore').onclick = () => { $('storefrontDialog').close(); importLutris(); };
    $('importGogStore').onclick = () => { $('storefrontDialog').close(); importGog(); };
    $('importEaStore').onclick = () => { $('storefrontDialog').close(); importEa(); };
    $('importUbisoftStore').onclick = () => { $('storefrontDialog').close(); importUbisoft(); };
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
        notify(t('notify.app.gameyfin_ok'));
      } catch(error) { $('storefrontGameyfinStatus').textContent = error.message; notify(error.message); }
    };
    $('importScummvmStore').onclick = async () => { try { const result = await api('/api/import/scummvm',{method:'POST',body:'{}'}); await refresh(); notify(`${result.added} ScummVM games imported`); } catch(error) { notify(error.message); } };
    $('importRpcs3Store').onclick = async () => { try { const result = await api('/api/import/rpcs3',{method:'POST',body:'{}'}); await refresh(); notify(`${result.added} RPCS3 games imported`); } catch(error) { notify(error.message); } };
    $('importVita3kStore').onclick = async () => { try { const result = await api('/api/import/vita3k',{method:'POST',body:'{}'}); await refresh(); notify(`${result.added} Vita3K games imported`); } catch(error) { notify(error.message); } };
    $('openThemeFolder').onclick = async () => { try { const result = await api('/api/themes/open-folder',{method:'POST',body:'{}'}); notify(t('notify.app.opened', {path: result.path})); } catch(error) { notify(error.message); } };
    $('injectRa').onclick = async () => { try { const result = await api('/api/ra/inject',{method:'POST',body:'{}'}); notify(t('notify.app.emulator_configs_updated', {count: result.updated.length})); } catch(error) { notify(error.message); } };
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
        notify(t('notify.app.duplicates_removed', {count: result.paths.length}));
      } catch(error) { notify(error.message); }
    };
    $('scanAllSaves').onclick = async () => { try { await api('/api/saves/scan/apply',{method:'POST',body:'{}'}); notify('info', t('notify.app.save_scan_queued')); } catch(error) { notify(error.message); } };
    $('bigBoxPause').onclick = event => { if (event.target === $('bigBoxPause')) $('bigBoxPause').hidden = true; };
    $('loadStorefrontCatalog').onclick = loadStorefrontCatalog;
    $('importStorefrontInstalled').onclick = () => importStorefrontCatalog(false);
    $('importStorefrontUninstalled').onclick = () => importStorefrontCatalog(true);
    $('addButton').onclick = () => openGameDialog(); $('addShelfButton').onclick = () => openGameDialog(null, {shelf:true}); $('importButton').onclick = importFolder; $('metadataButton').onclick = () => $('metadataDialog').showModal(); $('steamButton').onclick = importSteam; $('heroicButton').onclick = importHeroic; $('lutrisButton').onclick = importLutris; $('gogButton').onclick = importGog; $('eaButton').onclick = importEa; $('ubisoftButton').onclick = importUbisoft; $('arcadeButton').onclick = importArcade; $('pico8Button').onclick = importPico8; $('msu1Button').onclick = importMsu1; $('whdloadButton').onclick = importWhdload; $('arcadeRoomButton').onclick = () => openArcadeRoom({state:AppState, games:() => AppState.games, museumKioskEnabled:Boolean(AppState.appSettings.museum_kiosk_enabled && AppState.appSettings.museum_kiosk_pin_set), verifyMuseumPin, onShowGame:game => { AppState.selectedId = game.id; render(); }, onLaunch:game => launch(game)}); $('householdButton').onclick = openHousehold; $('emulatorsButton').onclick = openProfiles; $('settingsButton').onclick = openSettings; $('bigBoxButton').onclick = openBigBox; $('sessionsButton').onclick = openSessions; $('historyButton').onclick = openHistory; $('timeMachineButton').onclick = openTimeMachine; $('themesButton').onclick = openThemes; $('saveFilterButton').onclick = saveFilter; $('savePresetButton').onclick = savePreset; $('playlistsButton').onclick = openPlaylists; $('achievementsButton').onclick = openAchievements; $('pluginsButton').onclick = openPlugins; $('mediaButton').onclick = openMediaManager; $('healthButton').onclick = health; $('constellationButton').onclick = openConstellation; $('masteryButton').onclick = openMastery; $('bulkButton').onclick = bulkAction; $('backupButton').onclick = openBackups;
    $('doneTimeMachine').onclick = () => $('timeMachineDialog').close();
    // ── Accessible Tools menu ──────────────────────────────────────────
    const toolsWrap = $('toolsWrap');
    const toolsButton = $('toolsButton');
    const toolMenu = $('toolMenu');
    const getToolItems = () => [...toolMenu.querySelectorAll('[role="menuitem"]:not(:disabled)')];
    const toolsMenuOpen = () => toolsWrap.classList.contains('open');
    function openToolsMenu() {
      $('addMenuWrap')?.classList.remove('open');
      $('addMenuButton')?.setAttribute('aria-expanded', 'false');
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
    function bindAddMenu() {
      const wrap = $('addMenuWrap'), button = $('addMenuButton'), menu = $('addMenu');
      if (!wrap || !button || !menu) return;
      const open = () => {
        $('toolsWrap')?.classList.remove('open');
        $('toolsButton')?.setAttribute('aria-expanded', 'false');
        wrap.classList.add('open');
        button.setAttribute('aria-expanded', 'true');
        requestAnimationFrame(() => menu.querySelector('.menu-button')?.focus());
      };
      const close = (focusButton = false) => {
        wrap.classList.remove('open');
        button.setAttribute('aria-expanded', 'false');
        if (focusButton) button.focus();
      };
      button.addEventListener('click', () => wrap.classList.contains('open') ? close(false) : open());
      document.addEventListener('click', event => { if (!wrap.contains(event.target)) close(false); });
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && wrap.classList.contains('open') && !document.querySelector('dialog[open]')) {
          event.preventDefault();
          close(true);
        }
      });
      menu.addEventListener('click', () => button.focus(), { capture: true });
      menu.querySelectorAll('.menu-button').forEach(item => item.addEventListener('click', () => close(false)));
      menu.addEventListener('keydown', event => {
        const items = [...menu.querySelectorAll('.menu-button')];
        const index = items.indexOf(document.activeElement);
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          const delta = event.key === 'ArrowDown' ? 1 : -1;
          items[(index + delta + items.length) % items.length]?.focus();
        } else if (event.key === 'Home' || event.key === 'End') {
          event.preventDefault();
          items[event.key === 'Home' ? 0 : items.length - 1]?.focus();
        }
      });
      menu.querySelectorAll('[data-add-action]').forEach(item => {
        item.addEventListener('click', () => {
          const action = item.dataset.addAction;
          close(false);
          if (action === 'steam') importSteam();
          else if (action === 'heroic') importHeroic();
          else if (action === 'lutris') importLutris();
          else if (action === 'store') openStorefronts();
        });
      });
    }
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
    $('contextDna').onclick = () => { const game = AppState.games.find(item => item.id === AppState.contextGameId); closeContextMenu(); if (game) dnaMoreLikeThis(game.name); };
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
        notify(t('notify.app.stats_reset', {name: game.name}));
      } catch(error) { notify(error.message); }
    };
    $('contextRemove').onclick = () => { const game = AppState.games.find(item => item.id === AppState.contextGameId); closeContextMenu(); if (game) removeGame(game.id, game.name); };
    bindContextMenuA11y();
    document.addEventListener('click', event => {
      if (toolsMenuOpen() && !toolsWrap.contains(event.target)) closeToolsMenu(false);
    });
    if ($('scanEmulatorFolder')) $('scanEmulatorFolder').onclick = async () => {
      const folder = $('emulatorScanFolder').value.trim();
      if (!folder) return notify(t('notify.app.enter_rom_folder'));
      try {
        const result = await api('/api/emulators/scan',{method:'POST',body:JSON.stringify({folder})});
        await refresh();
        notify(t('notify.app.added_of_scanned', {added: result.added, found: result.found}));
      } catch(error) { notify(error.message); }
    };
    $('fullscreenButton').onclick = () => nativeFullscreen().catch(() => {});
    $('helpButton').onclick = () => openHelp();
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
    function typingTarget(target) {
      return target instanceof Element && Boolean(target.closest('input,textarea,select,[contenteditable="true"]'));
    }
    document.addEventListener('keydown', event => {
      if (matchesKey(event, keyAction('search')) && !typingTarget(event.target) && !document.querySelector('dialog[open]')) {
        const search = $('sidebarSearch');
        if (search) { event.preventDefault(); search.focus(); search.select(); }
      }
      if (matchesKey(event, keyAction('settings')) || (event.metaKey && event.key === ',')) { event.preventDefault(); openSettings(); }
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
      if (event.key === 'F1' && !event.target.closest?.('input,textarea,select,[contenteditable]') && !document.querySelector('dialog[open]')) {
        event.preventDefault(); openHelp();
      }
      if (event.key === '?' && !event.ctrlKey && !event.metaKey && !event.altKey
          && !event.target.closest?.('input,textarea,select,[contenteditable]') && !document.querySelector('dialog[open]')) {
        event.preventDefault(); openHelp();
      }
      if (event.key === 'Escape') {
        if (toolsMenuOpen()) closeToolsMenu(true);
      }
    });
    $('closeSettings').onclick = $('cancelSettings').onclick = () => $('settingsDialog').close();
    bindLaunchBoxMigration();
    bindEsdeImport();
    $('closeBigBoxMenu').onclick = closeBigBoxMenu;
    $('closeBigBoxPause').onclick = closeBigBoxPause;
    $('applyBigBoxMenu').onclick = applyBigBoxMenu;
    $('screenSaver').onclick = stopScreenSaver;
    $('closeMedia').onclick = () => { $('mediaDialog').close(); $('mediaDialog').querySelectorAll('img').forEach(el => el.remove()); };
    $('closeReader').onclick = () => { $('readerDialog').close(); resetReaderFrame(); };
    $('bigBox').onkeydown = event => {
      if (bigBoxTypingActive()) {
        if (matchesKey(event, keyAction('back'))) $('bigBoxHybridSearch').blur();
        return;
      }
      if (!$('screenSaver').hidden) {
        const game = AppState.screenSaverGame;
        stopScreenSaver();
        if (matchesKey(event, keyAction('play')) && game) activateCurrentGame(game);
        event.preventDefault();
        return;
      }
      if (!$('bigBoxMenu').hidden) {
        if (isBackKey(event)) closeBigBoxMenu();
        if (matchesKey(event, keyAction('play'))) applyBigBoxMenu();
        return;
      }
      AppState.bigBoxLastInput = performance.now();
      const snap = document.querySelector('.bigbox-video-snap');
      const inForm = event.target instanceof Element && event.target.closest('input,button,select,textarea');
      if (snap && !inForm) {
        if (event.key === 'f' && !event.ctrlKey && !event.altKey && !event.metaKey) {
          event.preventDefault();
          snap.requestFullscreen?.().catch(() => notify(t('notify.app.bigbox_fullscreen_unavailable')));
          return;
        }
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
          event.preventDefault();
          snap.volume = Math.min(1, Math.max(0, Number(snap.volume || 0) + (event.key === 'ArrowUp' ? 0.05 : -0.05)));
          return;
        }
      }
      const action = [
        ['pause', () => { if (AppState.runningGames.length) openBigBoxPause(); }],
        ['menu', () => openBigBoxMenu()],
        ['back', () => {
          if (event.repeat) return;
          if (!$('bigBoxPause').hidden) closeBigBoxPause();
          else startBigBoxBack();
        }],
        ['play', () => {
          const target = AppState.bigBoxGames[AppState.bigBoxIndex];
          if (target) activateCurrentGame(target);
        }],
        ['favorite', () => favoriteBigBox()],
        ['random', () => {
          AppState.bigBoxIndex = Math.floor(Math.random() * AppState.bigBoxGames.length);
          renderBigBox();
        }],
        ['page_left', () => moveBigBox(-10)],
        ['page_right', () => moveBigBox(10)],
      ].find(([name]) => name === 'back' ? isBackKey(event) : matchesKey(event, keyAction(name)));
      if (action) {
        event.preventDefault();
        action[1]();
        return;
      }
      if (event.key === ' ' && snap && !(event.target instanceof Element && event.target.closest('input,button,select,textarea'))) {
        event.preventDefault();
        if (snap.paused) snap.play().catch(() => {}); else snap.pause();
        return;
      }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') moveBigBox(-1);
      else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') moveBigBox(1);
    };
    document.addEventListener('keyup', event => {
      if (!$('bigBox') || $('bigBox').hidden) return;
      if (isBackKey(event)) releaseBigBoxBack();
    });

    // ?deeplink= dispatch map (S2 owns the shape). 'search' stays above — it
    // seeds the input before the
    // first render, so it must run before refresh().
    // A deeplink that names a game also scrolls its card into view, so the
    // window lands on the game instead of the top of the grid.
    function revealDeeplinkGame(gameId) {
      AppState.selectedId = gameId;
      render();
      document.querySelector('[data-game="' + gameId + '"]')?.scrollIntoView({ block: 'nearest' });
    }
    const DEEPLINK_ACTIONS = new Map([
      ['bigbox', () => openBigBox()],
      ['settings', () => openSettings()],
      ['showgame', params => {
        const resolved = resolveDeeplinkGameId(params.get('id'));
        if (resolved !== null) revealDeeplinkGame(resolved);
      }],
      // The native host resumes a session server-side and then loads the
      // window at ?deeplink=resume&id=; the page selects the game so the
      // window lands on the session it just resumed.
      ['resume', params => {
        const resolved = resolveDeeplinkGameId(params.get('id'));
        if (resolved !== null) revealDeeplinkGame(resolved);
      }],
      ['launch', params => {
        const resolved = resolveDeeplinkGameId(params.get('id'));
        if (resolved !== null) launch(resolved, null);
      }],
      ['recap', () => openSessionRecap()],
      ['moment', params => openMoment(params.get('id'))],
      ['clip', params => openClip(params.get('id'))],
    ]);
    function dispatchDeeplink(params) {
      const action = DEEPLINK_ACTIONS.get(params.get('deeplink'));
      if (typeof action === 'function') action(params);
    }

    function applyWindowsOnly() {
      const windows = nativeCaps.platform === 'win32';
      document.querySelectorAll('[data-windows-only]').forEach(element => { element.hidden = !windows; });
    }

    const deeplink = new URLSearchParams(location.search);
    const detailTarget = setupDetailsPopup();
    if (deeplink.get('deeplink') === 'search') $('sidebarSearch').value = deeplink.get('q') || '';
    // Restore the view context from the URL hash before the first render.
    applyHash();
    window.addEventListener('hashchange', () => { if (applyHash()) render(); });
    initNavigation();
    bindAddMenu();
    async function primeNotifications() {
      try {
        const result = await api('/api/notifications');
        const unread = Number(result.unread) || 0;
        AppState.unreadNotifications = unread;
        const button = $('notificationsButton');
        if (button) {
          button.dataset.unread = String(unread);
          button.setAttribute('aria-label', unread ? `${button.textContent.trim()} (${unread})` : button.textContent.trim());
        }
      } catch { /* the center reports its own errors when opened */ }
    }
    Promise.all([refresh(),loadTheme(),primeNotifications()]).then(async () => {
      applyFontFamily();
      if (AppState.startupErrors.length) {
        $('startupErrorText').textContent = AppState.startupErrors.join(' ');
        $('startupErrorBanner').hidden = false;
        $('startupErrorDismiss').onclick = () => { $('startupErrorBanner').hidden = true; };
      }
      if (detailTarget) {
        const game = AppState.games.find(item => String(item.game_id) === detailTarget || String(item.id) === detailTarget);
        if (game) {
          AppState.selectedId = game.id;
          render();
        } else {
          $('details').innerHTML = '<div class="detail-empty">This game is no longer in the library.<div class="extras"><button type="button" class="icon-button" id="detailsPopupClose">Close</button></div></div>';
          $('detailsPopupClose').onclick = () => window.close();
        }
      }
      initMood();
      focusSelectedGame();
    $('queueButton').onclick = () => openFeature('queue');
    $('tagsButton').onclick = () => openFeature('tags');
    $('notificationsButton').onclick = () => openFeature('notifications');
    $('webhooksButton').onclick = () => openFeature('webhooks');
    $('closeFeature').onclick = $('doneFeature').onclick = () => $('featureDialog').close();
      await detectNative().catch(() => {});
      applyWindowsOnly();
      connectSessionEvents();
      pollSessions();
      await runStartupStorefrontImports().catch(() => {});
      if (deeplink.get('deeplink') !== 'bigbox' && (AppState.appSettings.gamescope_guest || AppState.appSettings.bigbox_start_at_launch)) openBigBox();
      dispatchDeeplink(deeplink);
    }).catch(error => { showErrorBanner(error); if (detailTarget) $('details').innerHTML = `<div class="detail-empty">${escapeHtml(error.message || 'The library could not be loaded.')}</div>`; });
