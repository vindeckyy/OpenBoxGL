import { $, escapeHtml, defaultControllerMap, recentActivityValue } from './util.js';
import { AppState, media, filteredGames, api, notify, nativeFullscreen, nativeFullscreenOn } from './state.js';
import { refresh } from './library.js';
import { launch, openSessions } from './sessions.js';
import { openReader } from './reader.js';
import { loadAchievements } from './metadata.js';
import { installGameyfin, uninstallGameyfin } from './storefront.js';



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
      AppState.bigBoxBattery = undefined;
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
      nativeFullscreen().catch(() => {});
      requestAnimationFrame(pollGamepads);
    }
    function closeBigBox() {
      stopScreenSaver();
      $('bigBoxMenu').hidden = true;
      $('bigBox').hidden = true;
      if ($('bigBoxStartupVideo')) { $('bigBoxStartupVideo').pause(); $('bigBoxStartupVideo').hidden = true; }
      api('/api/bigbox/mode',{method:'POST',body:JSON.stringify({entering:false})}).catch(() => {});
      if (document.fullscreenElement || nativeFullscreenOn) nativeFullscreen().catch(() => {});
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
    function coverflowWindow(items, active, radius = 5) {
      if (items.length <= radius * 2 + 1) return items.map((item, index) => ({ item, index }));
      const window = [];
      for (let offset = -radius; offset <= radius; offset++) {
        const index = (active + offset + items.length) % items.length;
        window.push({ item: items[index], index, offset });
      }
      return window;
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
      // Battery queried once per BigBox open; cached in AppState.bigBoxBattery to avoid per-frame promise.
      if (AppState.bigBoxBattery === undefined) {
        AppState.bigBoxBattery = null;
        navigator.getBattery?.().then(status => { AppState.bigBoxBattery = status; renderBigBox(); }).catch(() => {});
      }
      if ($('bigBoxStatus')) {
        const status = AppState.bigBoxBattery;
        $('bigBoxStatus').innerHTML = status ? `<strong>${Math.round(status.level * 100)}%</strong> battery · ${escapeHtml(hint)}` : escapeHtml(hint);
      }
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
        $('bigBoxStage').innerHTML = `<div class="bigbox-platforms">${platforms.map(name => `<button class="bigbox-platform ${AppState.bigBoxPlatform === name ? 'active' : ''}" data-bigbox-platform="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join('')}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${ownedLabel}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${canAct ? '' : 'disabled'}>${playLabel}</button>${uninstallBtn}</div>`;
        document.querySelectorAll('[data-bigbox-platform]').forEach(button => button.onclick = () => { AppState.bigBoxPlatform = button.dataset.bigboxPlatform; AppState.bigBoxIndex = 0; renderBigBox(); });
      } else if (mode === 'coverflow') {
        $('bigBoxStage').className = 'bigbox-stage';
        const canAct = (game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed);
        const ownedLabel = game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : '';
        const uninstallBtn = game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:10px">Uninstall</button>' : '';
        const cards = coverflowWindow(AppState.bigBoxGames, AppState.bigBoxIndex);
        $('bigBoxStage').innerHTML = `<div class="coverflow-strip" data-coverflow-strip>${cards.map(({item,index,offset = 0}) => `<button class="coverflow-card jewel-case ${index === AppState.bigBoxIndex ? 'active' : ''}" data-coverflow="${index}" style="--coverflow-offset:${offset}" aria-label="Open ${escapeHtml(item.name)}">${item.has_cover ? `<img src="${media(item,'cover')}" alt="" loading="lazy" decoding="async">` : `<div class="cover-title">${escapeHtml(item.name)}</div>`}</button>`).join('')}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${ownedLabel}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${canAct ? '' : 'disabled'}>${playLabel}</button>${uninstallBtn}</div>`;
        document.querySelector('[data-coverflow-strip]')?.addEventListener('click', event => {
          const button = event.target.closest('[data-coverflow]');
          if (!button) return;
          AppState.bigBoxIndex = Number(button.dataset.coverflow);
          AppState.selectedId = AppState.bigBoxGames[AppState.bigBoxIndex].id;
          renderBigBox();
        });
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
      $('bigBoxPauseMeta').textContent = `${session.paused ? 'Paused' : 'Running'} · started ${String(session.started || '').replace('T',' ')}`;
      $('bigBoxPauseActions').innerHTML = `<button class="primary" data-pause-action="${session.launch_id}:${session.paused ? 'resume' : 'pause'}">${session.paused ? 'Resume' : 'Pause'}</button><button class="icon-button" data-pause-action="${session.launch_id}:stop">Exit game</button>${game?.documents.map((item,index) => `<button class="icon-button" data-pause-doc="${game.id}:${index}">Read ${escapeHtml(item.name)}</button>`).join('') || ''}${AppState.raConfigured ? `<button class="icon-button" id="pauseAchievements">Achievements</button>` : ''}`;
      document.querySelectorAll('[data-pause-action]').forEach(button => button.onclick = async () => {
        const [launch_id,action] = button.dataset.pauseAction.split(':');
        await api('/api/session/control',{method:'POST',body:JSON.stringify({launch_id,action})});
        $('bigBoxPause').hidden = true;
        openBigBoxPause();
      });
      document.querySelectorAll('[data-pause-doc]').forEach(button => button.onclick = () => {
        const [gameId,index] = button.dataset.pauseDoc.split(':');
        const targetGame = AppState.games.find(item => item.id === Number(gameId));
        if (targetGame) openReader(targetGame,Number(index));
      });
      if ($('pauseAchievements')) $('pauseAchievements').onclick = () => { $('bigBoxPause').hidden = true; openAchievements(); };
      $('bigBoxPause').hidden = false;
    }
    function startScreenSaver() {
      const visible = filteredBigBoxGames();
      if (!visible.length) return;
      const game = visible[Math.floor(Math.random() * visible.length)];
      AppState.screenSaverGame = game;
      $('screenSaverVideo').src = media(game,'video');
      $('screenSaverTitle').textContent = game.name;
      $('screenSaverPlatform').textContent = game.platform || 'Game';
      $('screenSaver').hidden = false;
    }
    function stopScreenSaver() {
      if ($('screenSaver').hidden) return;
      $('screenSaver').hidden = true;
      $('screenSaverVideo').pause();
      $('screenSaverVideo').removeAttribute('src');
      AppState.screenSaverGame = null;
      AppState.bigBoxLastInput = performance.now();
    }
    async function favoriteBigBox() {
      const game = AppState.bigBoxGames[AppState.bigBoxIndex];
      if (game) {
        try {
          await api('/api/favorite',{method:'POST',body:JSON.stringify({id:game.id})});
          await refresh();
          AppState.bigBoxGames = filteredBigBoxGames();
          if (!AppState.bigBoxGames.length) { closeBigBox(); notify('The current view is now empty'); return; }
          AppState.bigBoxIndex = Math.max(0,AppState.bigBoxGames.findIndex(g => g.id === game.id));
          renderBigBox();
        } catch(error) { notify(error.message); }
      }
    }
    function pollGamepads() {
      if ($('bigBox').hidden) return;
      const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
      const pad = pads[0];
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
          const selects = [$('bigBoxFilter'),$('bigBoxSort'),$('bigBoxQuickPreset'),$('bigBoxRaFilter')].filter(Boolean);
          let active = selects.indexOf(document.activeElement);
          if (active < 0) active = 0;
          if (edge('up')) {
            active = (active - 1 + selects.length) % selects.length;
            selects[active].focus();
          }
          if (edge('down')) {
            active = (active + 1) % selects.length;
            selects[active].focus();
          }
          if (edge('left') || edge('right')) {
            const select = selects[active], change = edge('left') ? -1 : 1;
            if (select && select.options.length) {
              select.selectedIndex = (select.selectedIndex + change + select.options.length) % select.options.length;
            }
          }
          if (edge('play')) applyBigBoxMenu();
          if (edge('back') || edge('menu')) closeBigBoxMenu();
        } else {
          if (edge('left') || edge('up')) moveBigBox(-1);
          if (edge('right') || edge('down')) moveBigBox(1);
          if (edge('play')) {
            const target = AppState.bigBoxGames[AppState.bigBoxIndex];
            if (target) launch(target.id);
          }
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

export { openBigBox, closeBigBox, filteredBigBoxGames, openBigBoxMenu, closeBigBoxMenu, applyBigBoxMenu, moveBigBox, renderBigBox, applyLibraryMusic, openBigBoxPause, startScreenSaver, stopScreenSaver, favoriteBigBox, pollGamepads };
