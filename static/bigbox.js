import { $, escapeHtml, defaultControllerMap, recentActivityValue, prefersReducedMotion } from './util.js';
import { firstEdge, wheelStepAllowed, screenSaverStats } from './pure.js';
import { AppState, media, coverSource, filteredGames, api, token, notify, gameForSession, nativeFullscreen, nativeFullscreenOn, nativePickFile, platformCategoryFor } from './state.js';
import { createPointerNavigator } from './pointer_nav.js';
import { readBattery, batteryLabel } from './battery.js';
import { configureThemeSounds, startThemeMusic, stopThemeMusic, playThemeSound } from './sfx.js';
import { refresh } from './library.js';
import { launch, openSessions } from './sessions.js';
import { openReader } from './reader.js';
import { loadAchievements, renderAchievementTabs } from './metadata.js';
import { openDialog } from './dialogs.js';
import { installGameyfin, uninstallGameyfin } from './storefront.js';
import { applyMoodForGame, clearMood } from './mood.js';
import { openParty, partyOverlayOpen, partyGamepad } from './party.js';
import { captureMomentInteractive } from './moments.js';
import { captureClip } from './clips.js';
import { openSetupCenter } from './setup.js';
import { t } from './i18n.js';
import { registerGamepadSurface, ensureGamepadLoop, stopGamepadLoop, syncGamepadLoop } from './gamepad.js';
import { dnaSearchMode, setDnaSearchMode, scheduleBigBoxSmartSearch, cancelBigBoxSmartSearch, dnaMoreLikeThisBigBox } from './dna.js';
import { renderCoverModel, bindCoverModel, coverModelFor } from './model3d.js';



const ACTION_ORDER = ['pause', 'menu', 'back', 'play', 'favorite', 'random', 'pageLeft', 'pageRight'];
let pointerNavigator = null;
let pointerNavigatorConfig = '';

function backHoldSeconds() {
  const value = Number(AppState.appSettings.bigbox_back_hold_seconds);
  return Number.isFinite(value) ? Math.min(2, Math.max(0, value)) : 0.8;
}
function startBigBoxBack() {
  playThemeSound('back');
  if (backHoldSeconds() <= 0) {
    closeBigBox();
    return;
  }
  AppState.backPressedAt = performance.now();
}
function releaseBigBoxBack() {
  if (!AppState.backPressedAt) return;
  const held = (performance.now() - AppState.backPressedAt) / 1000;
  AppState.backPressedAt = 0;
  if (held >= backHoldSeconds()) openBigBoxMenu();
  else closeBigBox();
}

    async function activateCurrentGame(game, trigger) {
      if (!game) return;
      playThemeSound('select');
      if (game.steam_installable) {
        try {
          await api('/api/game/store-action', { method: 'POST', body: JSON.stringify({ id: game.id, game_id: game.game_id, action: 'install' }) });
          notify(t('notify.app.steam_install_queued'));
        } catch (error) { notify(error.message); }
        return;
      }
      if (game.gameyfin_id && !game.store_installed) {
        await installGameyfin(game);
        return;
      }
      await launch(game, trigger || $('bigBoxPlay') || $('playButton'));
    }

    function bigBoxTypingActive() {
      const search = $('bigBoxHybridSearch');
      return search && !search.hidden && document.activeElement === search;
    }

    // S18: `#bigBox` is a <section role="dialog" aria-modal="true">, not a native
    // <dialog>, so it gets no modal behavior at all -- declaring aria-modal
    // without making the rest of the page inert is a lie the browser cannot
    // enforce. Tab walked straight into the topbar and the grid behind the
    // overlay. `inert` is the missing half: it removes the rest of the document
    // from the focus order, the accessibility tree and hit-testing in one
    // attribute.
    //
    // The keep-out list is explicit rather than "everything but #bigBox", because
    // the toast container is a top-layer popover that must stay reachable -- an
    // Undo button that vanishes because Big Box opened is B2's bug returning by
    // another route. `#gamepadHint` and `#errorBanner` are announced content
    // and must not be pulled out of the accessibility tree either.
    const INERT_KEEP_VISIBLE = new Set(['bigBox', 'toasts', 'toastLive', 'gamepadHint', 'errorBanner']);
    let inertedNodes = [];
    function setBackgroundInert(inert) {
      if (!inert) {
        for (const el of inertedNodes) {
          el.removeAttribute('inert');
          if (el.getAttribute('data-inert-by-bigbox') === '1') el.removeAttribute('aria-hidden');
        }
        inertedNodes = [];
        return;
      }
      inertedNodes = [];
      for (const el of document.body.children) {
        if (el.id && INERT_KEEP_VISIBLE.has(el.id)) continue;
        if (el.tagName === 'DIALOG') continue;   // a native modal manages its own
        if (el.contains($('bigBox'))) continue;
        el.setAttribute('inert', '');
        el.setAttribute('aria-hidden', 'true');
        el.setAttribute('data-inert-by-bigbox', '1');
        inertedNodes.push(el);
      }
    }

    // Releasing this is easy to forget, and a forgotten release is much worse than
    // the problem `inert` was added to solve. Any path that hides #bigBox
    // without going through closeBigBox() -- the empty-view branch, a deeplink,
    // the gamepad loop stopping on blur -- leaves the entire page unfocusable:
    // buttons render, clicks still fire, and `.focus()` is silently a no-op, so
    // every dialog's focus restore lands on <body> and keyboard navigation dies
    // for the rest of the session. So the release is *observed* rather than
    // called, the same way dialogs.js watches `dialog[open]`.
    function watchBigBoxVisibility() {
      const bigBox = $('bigBox');
      if (!bigBox) return;
      new MutationObserver(() => { if (bigBox.hidden) setBackgroundInert(false); })
        .observe(bigBox, { attributes: true, attributeFilter: ['hidden'] });
    }
    if ($('bigBox')) watchBigBoxVisibility();
    else document.addEventListener('DOMContentLoaded', watchBigBoxVisibility, { once: true });

    function syncPointerNavigator() {
      const enabled = AppState.appSettings.pointer_nav_enabled !== false;
      const config = JSON.stringify({
        enabled,
        sensitivity: AppState.appSettings.spinner_sensitivity ?? 6,
        wheel: AppState.appSettings.mouse_wheel_nav !== false,
      });
      if (config === pointerNavigatorConfig) return;
      pointerNavigatorConfig = config;
      pointerNavigator?.destroy();
      pointerNavigator = null;
      if (!enabled || !$('bigBox')) return;
      pointerNavigator = createPointerNavigator({
        surface: $('bigBox'),
        sensitivity: AppState.appSettings.spinner_sensitivity ?? 6,
        wheelEnabled: AppState.appSettings.mouse_wheel_nav !== false,
        dragEnabled: true,
        onStep: (delta, source) => moveBigBox(delta, { wheel: source === 'wheel' }),
        onPage: delta => moveBigBox(delta * 10),
        onActivate: () => {
          const game = AppState.bigBoxGames[AppState.bigBoxIndex];
          if (game && $('bigBox') && !$('bigBox').hidden) activateCurrentGame(game);
        },
        onActivity: () => {
          AppState.bigBoxLastInput = performance.now();
          if (!$('screenSaver').hidden) stopScreenSaver();
        },
      });
    }
    function openBigBox() {
      AppState.bigBoxFilter = 'all';
      AppState.bigBoxSort = 'title';
      AppState.bigBoxHybridQuery = '';
      // The hybrid platform tab is reset with the other filters: a platform
      // remembered from an earlier open could match nothing now, and an empty
      // list sends openBigBox to the setup wizard instead of Big Box.
      AppState.bigBoxPlatform = 'all';
      AppState.bigBoxSearchMode = false;
      if ($('bigBoxHybridSearch')) $('bigBoxHybridSearch').value = '';
      AppState.games.forEach(item => { delete item._dnaWhy; });
      AppState.bigBoxGames = filteredBigBoxGames();
      if (!AppState.bigBoxGames.length) {
        // Keep Big Box hidden when there is nothing to show: a previous open
        // may have left it visible, but the empty view defers to the wizard.
        $('bigBox').hidden = true;
        notify('info', t('bigbox.empty_view_setup'));
        openSetupCenter();
        return;
      }
      AppState.bigBoxIndex = Math.max(0,AppState.bigBoxGames.findIndex(game => game.id === AppState.selectedId));
      AppState.bigBoxLastInput = performance.now();
      AppState.gamepadState = {};
      syncPointerNavigator();
      // Query battery once per BigBox open rather than per render frame.
      AppState.bigBoxBattery = null;
      readBattery().then(status => { AppState.bigBoxBattery = status; renderBigBox(); }).catch(() => {});
      $('bigBox').hidden = false;
      setBackgroundInert(true);
      configureThemeSounds(AppState.appSettings.theme, { enabled: AppState.appSettings.bigbox_sounds_enabled !== false })
        .then(() => startThemeMusic())
        .catch(() => {});
      $('bigBox').focus();
      const startup = $('bigBoxStartupVideo');
      if (startup && AppState.appSettings.bigbox_startup_video) {
        startup.src = `/api/bigbox/startup-video?token=${encodeURIComponent(token)}`;
        startup.classList.remove('startup-video-done');
        startup.loop = AppState.appSettings.bigbox_startup_video_loop === true;
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
      ensureGamepadLoop();
    }
    async function closeBigBox() {
      stopScreenSaver();
      stopThemeMusic();
      syncGamepadLoop();
      $('bigBoxMenu').hidden = true;
      setBackgroundInert(false);
      // S30: the video snap loops forever after close and library music stays
      // pinned at 0.1 volume, so every later song in the session played at a
      // tenth of its intended level. `clearVideoSnap()` was simply never called
      // on the way out, while four render paths already called it on the way in.
      clearVideoSnap();
      if ($('bigBoxStartupVideo')) { $('bigBoxStartupVideo').pause(); $('bigBoxStartupVideo').hidden = true; }
      if ($('bigBoxBackdrop')) { $('bigBoxBackdrop').pause(); $('bigBoxBackdrop').hidden = true; $('bigBoxBackdrop').removeAttribute('src'); }
      const game = AppState.games.find(item => item.id === AppState.selectedId);
      applyMoodForGame(game).catch(() => {});
      // Shutdown applications are started by the server on the leaving edge.
      // Await that request (bounded to 3 s) so the window does not disappear
      // before the command launch has been accepted.
      try {
        await Promise.race([
          api('/api/bigbox/mode',{method:'POST',body:JSON.stringify({entering:false})}),
          new Promise(resolve => setTimeout(resolve, 3000)),
        ]);
      } catch (error) {
        notify(error.message);
      }
      $('bigBox').hidden = true;
      if (document.fullscreenElement || nativeFullscreenOn) nativeFullscreen().catch(() => {});
    }
    function applyBigBoxDnaResults(payload) {
      const results = (payload && payload.results) || [];
      if (payload && payload.building) {
        if ($('bigBoxStatus')) $('bigBoxStatus').textContent = t('dna.building', { pct: 0 });
        AppState.bigBoxGames = [];
        AppState.bigBoxIndex = 0;
        renderBigBox();
        return;
      }
      const byId = new Map();
      AppState.games.forEach(item => {
        if (item.game_id != null) byId.set(String(item.game_id), item);
        byId.set(String(item.id), item);
      });
      const games = [];
      results.forEach(entry => {
        const game = byId.get(String(entry.game_id ?? ''));
        if (game) { game._dnaWhy = entry.why || []; games.push(game); }
      });
      // In smart mode an empty result set is a real answer ("no matches"),
      // not a reason to fall back to the title-filtered list.
      AppState.bigBoxGames = games;
      if (!games.length && $('bigBoxStatus')) $('bigBoxStatus').textContent = t('dna.no_results');
      AppState.bigBoxIndex = 0;
      renderBigBox();
    }
    function filteredBigBoxGames({ ignorePlatform = false } = {}) {
      const hiddenPlaylists = new Set(AppState.appSettings.bigbox_hidden_playlists || []);
      if (AppState.activePlaylist && hiddenPlaylists.has(AppState.activePlaylist)) AppState.activePlaylist = '';
      const hiddenPlatforms = new Set(AppState.appSettings.bigbox_hidden_platforms || []);
      const hiddenCategories = new Set(AppState.appSettings.bigbox_hidden_categories || []);
      let result = filteredGames().filter(game => !game.hide_in_bigbox && !hiddenPlatforms.has(game.platform || 'Unspecified') && !hiddenCategories.has(platformCategoryFor(game)));
      if (!ignorePlatform && AppState.bigBoxPlatform !== 'all') result = result.filter(game => game.platform === AppState.bigBoxPlatform);
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
      ensureBigBoxHealthTile();
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
    // ── Library health tile (Flagship 8, H5) ───────────────────────────────
    // Read-only on the couch: cached score + worst dimension, controller-
    // navigable breakdown, and a "Re-scan library" action. No fix flows here.
    const HEALTH_DIMENSIONS = ['file_integrity', 'duplicates', 'artwork', 'metadata', 'launch_readiness'];
    function ensureBigBoxHealthTile() {
      const panel = document.querySelector('#bigBoxMenu .bigbox-menu-panel .dialog-actions');
      if (!panel || panel.querySelector('#bigBoxHealthButton')) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.id = 'bigBoxHealthButton';
      button.className = 'icon-button';
      button.dataset.i18n = 'health.open';
      button.textContent = t('health.open');
      button.onclick = () => { closeBigBoxMenu(); openBigBoxHealth(); };
      panel.appendChild(button);
    }
    function healthTileDialog() {
      let dialog = $('bigBoxHealthDialog');
      if (dialog) return dialog;
      dialog = document.createElement('dialog');
      dialog.id = 'bigBoxHealthDialog';
      dialog.setAttribute('aria-modal', 'true');
      dialog.innerHTML = `<div class="dialog-head"><h2 data-i18n="health.title">${escapeHtml(t('health.title'))}</h2><button type="button" class="icon-button" data-health-close aria-label="${escapeHtml(t('health.dismiss'))}">×</button></div><div class="dialog-body" data-health-body><p class="description">${escapeHtml(t('common.loading'))}</p></div><div class="dialog-actions"><button type="button" class="primary" data-health-rescan>${escapeHtml(t('health.rescan'))}</button><button type="button" class="icon-button" data-health-close>${escapeHtml(t('health.dismiss'))}</button></div>`;
      document.body.appendChild(dialog);
      dialog.querySelectorAll('[data-health-close]').forEach(button => button.onclick = () => { dialog.close(); $('bigBox')?.focus(); });
      dialog.querySelector('[data-health-rescan]').onclick = async () => {
        try {
          await api('/api/v2/library/health/scan', { method: 'POST', body: '{}' });
          notify(t('health.scan_queued'));
        } catch (error) { notify(error.message); }
      };
      return dialog;
    }
    async function openBigBoxHealth() {
      const dialog = healthTileDialog();
      const body = dialog.querySelector('[data-health-body]');
      if (!dialog.open) dialog.showModal();
      try {
        const snapshot = await api('/api/v2/library/health');
        if (!snapshot.scanned) {
          body.innerHTML = `<p class="description">${escapeHtml(t('health.no_scan'))}</p>`;
        } else {
        const dims = snapshot.dimensions || {};
        const worst = HEALTH_DIMENSIONS.reduce((acc, dim) => (!acc || (dims[dim]?.score ?? 100) < (dims[acc]?.score ?? 100) ? dim : acc), null);
        body.innerHTML = `<div class="health-hero">
          <div class="health-score ${snapshot.score >= 85 ? 'health-great' : snapshot.score >= 60 ? 'health-ok' : 'health-poor'}">${snapshot.score}</div>
          <div class="health-hero-meta"><div class="description">${escapeHtml(t('health.computed', { count: snapshot.game_count }))}</div>
          ${worst ? `<div class="description">${escapeHtml(t('health.weakest', { dimension: t(`health.dim_${worst}`, worst) }))}</div>` : ''}</div>
        </div>
        <div class="health-dims">${HEALTH_DIMENSIONS.map(dim => {
          const sub = dims[dim]?.score ?? 0;
          return `<div class="health-dim"><span class="health-dim-name">${escapeHtml(t(`health.dim_${dim}`, dim))}</span><span class="health-bar"><span class="health-bar-fill" style="width:${sub}%"></span></span><span class="health-dim-score">${sub}</span></div>`;
        }).join('')}</div>`;
        }
      } catch (error) {
        body.innerHTML = `<p class="description">${escapeHtml(error.message)}</p>`;
      }
      dialog.querySelector('[data-health-rescan]')?.focus();
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
      if (!AppState.bigBoxGames.length) { notify(t('notify.bigbox.no_matches')); return; }
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
    function moveBigBox(change, { wheel = false } = {}) {
      if (wheel && !wheelStepAllowed(performance.now(), AppState.bigBoxLastMove || 0, AppState.appSettings.bigbox_wheel_speed)) return;
      AppState.bigBoxLastMove = performance.now();
      // S32: a filter that matches nothing left the list empty, the modulo went
      // to NaN, and `bigBoxGames[NaN].id` threw -- every frame, from the gamepad
      // tick, where the error is swallowed and the stage simply stops advancing.
      // A zero-length list has no next card, so the only correct answer is to do
      // nothing and let the already-rendered empty view stand.
      const count = AppState.bigBoxGames.length;
      if (!count) return;
      AppState.bigBoxIndex = (AppState.bigBoxIndex + change % count + count) % count;
      const game = AppState.bigBoxGames[AppState.bigBoxIndex];
      if (!game) return;
      AppState.selectedId = game.id;
      AppState.bigBoxLastInput = performance.now();
      playThemeSound('move');
      renderBigBox();
      // The stage is rebuilt on every step, so slide the new content in from the direction of travel
      // (ADR 0063). Durations are tokens, so reduced motion makes this instant.
      const stage = $('bigBoxStage');
      if (stage && change) {
        stage.classList.remove('nav-next', 'nav-prev');
        void stage.offsetWidth;
        stage.classList.add(change > 0 ? 'nav-next' : 'nav-prev');
      }
    }

    // --- Video snap player (1.9.0) ---
    // ponytail: single reused <video> element, 600ms debounce, ducks BGM,
    // respects prefers-reduced-motion, falls back to static cover.
    let _videoSnapEl = null;
    let _videoSnapControls = null;
    let _videoSnapTimer = 0;
    let _videoSnapGameId = null;
    const _reducedMotion = () => prefersReducedMotion();

    function videoVolume() {
      const value = Number(AppState.appSettings.bigbox_video_volume);
      return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0.8;
    }

    function mountVideoControls(video) {
      if (AppState.appSettings.bigbox_video_controls === false) return null;
      const cover = video.closest('.bigbox-cover');
      if (!cover) return null;
      const bar = document.createElement('div');
      bar.className = 'bigbox-video-controls';
      bar.setAttribute('role', 'group');
      bar.setAttribute('aria-label', 'Video controls');
      bar.innerHTML = '<button type="button" data-video-toggle aria-label="Pause video">Pause</button><input type="range" min="0" max="1000" value="0" aria-label="Seek video"><button type="button" data-video-mute aria-label="Mute video">Mute</button><button type="button" data-video-fullscreen aria-label="Full screen video">Full screen</button>';
      cover.appendChild(bar);
      const toggle = bar.querySelector('[data-video-toggle]');
      const seek = bar.querySelector('input[type="range"]');
      const mute = bar.querySelector('[data-video-mute]');
      const full = bar.querySelector('[data-video-fullscreen]');
      let hideTimer = 0;
      const show = () => {
        bar.classList.add('is-active');
        if (hideTimer) clearTimeout(hideTimer);
        scheduleHide();
      };
      const scheduleHide = () => {
        if (hideTimer) clearTimeout(hideTimer);
        hideTimer = setTimeout(() => { bar.classList.remove('is-active'); hideTimer = 0; }, 2000);
      };
      cover.addEventListener('pointerenter', show);
      cover.addEventListener('pointermove', show);
      cover.addEventListener('pointerleave', scheduleHide);
      bar.addEventListener('focusin', show);
      bar.addEventListener('focusout', event => {
        if (!bar.contains(event.relatedTarget)) scheduleHide();
      });
      const sync = () => {
        toggle.textContent = video.paused ? 'Play' : 'Pause';
        toggle.setAttribute('aria-label', video.paused ? 'Play video' : 'Pause video');
        mute.textContent = video.muted ? 'Unmute' : 'Mute';
        mute.setAttribute('aria-label', video.muted ? 'Unmute video' : 'Mute video');
        if (seek && Number.isFinite(video.duration) && video.duration > 0) {
          seek.value = String(Math.round(video.currentTime / video.duration * 1000));
        }
      };
      toggle.onclick = event => { event.stopPropagation(); if (video.paused) video.play().catch(() => {}); else video.pause(); sync(); };
      mute.onclick = event => { event.stopPropagation(); video.muted = !video.muted; sync(); };
      seek.oninput = event => {
        event.stopPropagation();
        if (Number.isFinite(video.duration) && video.duration > 0) video.currentTime = Number(seek.value) / 1000 * video.duration;
      };
      full.onclick = event => { event.stopPropagation(); video.requestFullscreen?.().catch(() => notify(t('notify.app.bigbox_fullscreen_unavailable'))); };
      video.addEventListener('play', sync);
      video.addEventListener('pause', sync);
      video.addEventListener('timeupdate', sync);
      sync();
      return bar;
    }

    function clearVideoSnap() {
      if (_videoSnapTimer) { clearTimeout(_videoSnapTimer); _videoSnapTimer = 0; }
      if (_videoSnapEl) { _videoSnapEl.pause(); _videoSnapEl.removeAttribute('src'); _videoSnapEl.load(); _videoSnapEl = null; }
      _videoSnapControls?.remove();
      _videoSnapControls = null;
      _videoSnapGameId = null;
      if (AppState.libraryBgm && AppState.libraryBgm.volume < 0.4) {
        AppState.libraryBgm.volume = AppState.appSettings.video_bgm_mix ? 0.35 : 0.6;
      }
    }

    function scheduleVideoSnap(game) {
      if (_reducedMotion()) return;
      if (_videoSnapTimer) clearTimeout(_videoSnapTimer);
      // media() always builds a URL; gate on the has_video flag so games
      // without a snap never get a doomed <video> element.
      if (!game.has_video) { clearVideoSnap(); return; }
      const videoUrl = media(game, 'video');
      _videoSnapTimer = setTimeout(() => {
        _videoSnapTimer = 0;
        const coverEl = document.querySelector('.bigbox-cover');
        if (!coverEl) return;
        if (_videoSnapGameId === game.id && _videoSnapEl) {
          if (!_videoSnapEl.isConnected) {
            coverEl.appendChild(_videoSnapEl);
            _videoSnapControls = mountVideoControls(_videoSnapEl);
          }
          return;
        }
        clearVideoSnap();
        const el = document.createElement('video');
        el.muted = !AppState.appSettings.video_bgm_mix;
        el.loop = true;
        el.playsInline = true;
        el.volume = videoVolume();
        el.className = 'bigbox-video-snap';
        el.src = videoUrl;
        coverEl.appendChild(el);
        _videoSnapEl = el;
        _videoSnapGameId = game.id;
        _videoSnapControls = mountVideoControls(el);
        const start = () => el.play().catch(() => {
          // Autoplay policy: retry muted once, then leave the static cover.
          el.muted = true;
          el.play().catch(() => {});
          if (!AppState.appSettings.video_bgm_mix) return;
          notify(t('notify.app.video_audio_click'));
        });
        start();
        // Duck BGM while video plays
        if (AppState.libraryBgm && AppState.appSettings.video_bgm_mix) AppState.libraryBgm.volume = 0.1;
      }, 600);
    }

    function syncBigBoxBackdrop(game) {
      const backdrop = $('bigBoxBackdrop');
      if (!backdrop) return;
      const platformPath = AppState.appSettings.platform_videos?.[game?.platform || ''];
      const enabled = AppState.appSettings.bigbox_video_backdrop === true;
      const source = enabled ? (platformPath ? `/api/platform/video?platform=${encodeURIComponent(game.platform)}&token=${encodeURIComponent(token)}` : (game?.has_video ? media(game, 'video') : '')) : '';
      if (!source) {
        backdrop.hidden = true;
        backdrop.pause();
        backdrop.removeAttribute('src');
        return;
      }
      if (backdrop.getAttribute('src') !== source) backdrop.src = source;
      backdrop.hidden = false;
      backdrop.play().catch(() => {});
    }
    // --- end video snap ---
    function renderBigBox() {
      const game = AppState.bigBoxGames[AppState.bigBoxIndex];
      applyMoodForGame(game).catch(() => {});
      syncBigBoxBackdrop(game);
      if (!game) {
        // An empty list (a search or smart search with no matches) used to
        // leave the previous game on stage with its Play button still bound
        // to that game, so clicking Play launched a game that was not a match.
        clearVideoSnap();
        if ($('bigBoxStage')) $('bigBoxStage').innerHTML = '';
        return;
      }
      const hint = AppState.appSettings.controller_prompt_hint || 'A Play · B Back · M Menu';
      const battery = $('bigBoxBattery');
      if (battery) {
        const label = AppState.appSettings.battery_indicator === false ? '' : batteryLabel(AppState.bigBoxBattery);
        battery.hidden = !label;
        battery.textContent = label;
        battery.setAttribute('aria-label', label ? `Battery ${label}` : '');
      }
      if ($('bigBoxStatus')) {
        $('bigBoxStatus').textContent = hint;
      }
      const mode = AppState.appSettings.bigbox_mode || 'stage';
      if ($('bigBoxHybridSearch')) $('bigBoxHybridSearch').hidden = mode !== 'hybrid';
      if ($('bigBoxDnaToggle')) { $('bigBoxDnaToggle').hidden = mode !== 'hybrid'; setDnaSearchMode(dnaSearchMode(), { search: false }); }
      const playLabel = game.steam_installable
        ? '⬇ INSTALL'
        : game.gameyfin_id && !game.store_installed
          ? '⬇ INSTALL'
          : (AppState.appSettings.dynamic_play_button === false ? '▶ PLAY' : (game.versions || []).length ? '▶ PLAY DEFAULT' : (game.applications || []).length ? '▶ PLAY GAME' : '▶ PLAY');
      const steamRepairBtn = game.steam_repairable
        ? '<button class="icon-button" id="bigBoxSteamRepair" style="margin-left:var(--space-sm)">Verify Steam files</button>'
        : '';
      if (mode === 'hybrid') {
        clearVideoSnap();
        // The tab row lists every platform, not just the ones left after the
        // platform filter -- built from bigBoxGames it collapsed to the one
        // selected tab after the first click, with no way to switch back.
        const platforms = [...new Set(filteredBigBoxGames({ ignorePlatform: true }).map(item => item.platform || 'Unspecified'))].sort();
        if (AppState.bigBoxPlatform === 'all' && platforms.length) {
          // Narrow the list to the chosen tab too; navigating an unfiltered
          // list off this platform snapped straight back to its first game.
          AppState.bigBoxPlatform = platforms.includes(game.platform || 'Unspecified') ? (game.platform || 'Unspecified') : platforms[0];
          if (!(dnaSearchMode() === 'smart' && AppState.bigBoxHybridQuery)) {
            AppState.bigBoxGames = filteredBigBoxGames();
            AppState.bigBoxIndex = Math.max(0, AppState.bigBoxGames.findIndex(item => item.id === game.id));
            return renderBigBox();
          }
        }
        const platformGames = AppState.bigBoxGames.filter(item => (item.platform || 'Unspecified') === AppState.bigBoxPlatform);
        if (platformGames.length && !platformGames.some(item => item.id === game.id)) {
          AppState.bigBoxIndex = AppState.bigBoxGames.findIndex(item => item.id === platformGames[0].id);
          return renderBigBox();
        }
        $('bigBoxStage').className = 'bigbox-stage bigbox-hybrid';
        const canAct = (game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed) || game.steam_installable;
        const ownedLabel = game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : '';
        const uninstallBtn = game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:var(--space-sm)">Uninstall</button>' : '';
        $('bigBoxStage').innerHTML = `<div class="bigbox-platforms">${platforms.map(name => `<button class="bigbox-platform ${AppState.bigBoxPlatform === name ? 'active' : ''}" data-bigbox-platform="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join('')}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${ownedLabel}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p>${game._dnaWhy && game._dnaWhy.length ? `<p class="bigbox-dna-why">${game._dnaWhy.map(chip => `<span class="dna-chip">${escapeHtml(chip)}</span>`).join('')}</p>` : ''}<button class="bigbox-play" id="bigBoxPlay" ${canAct ? '' : 'disabled'}>${playLabel}</button>${steamRepairBtn}${uninstallBtn}<button class="icon-button" id="bigBoxDnaSimilar" style="margin-left:var(--space-sm)">${escapeHtml(t('dna.more_like_this'))}</button></div>`;
        document.querySelectorAll('[data-bigbox-platform]').forEach(button => button.onclick = () => {
          AppState.bigBoxPlatform = button.dataset.bigboxPlatform;
          // In smart mode with a query, re-run smart search (platform goes
          // to the DNA filters) instead of dropping to title filtering.
          if (dnaSearchMode() === 'smart' && AppState.bigBoxHybridQuery) {
            $('bigBoxHybridSearch')?.dispatchEvent(new Event('input', { bubbles: true }));
            return;
          }
          AppState.bigBoxGames = filteredBigBoxGames();
          AppState.bigBoxIndex = 0;
          renderBigBox();
        });
      } else if (mode === 'coverflow') {
        clearVideoSnap();
        $('bigBoxStage').className = 'bigbox-stage';
        const canAct = (game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed) || game.steam_installable;
        const ownedLabel = game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : '';
        const uninstallBtn = game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:var(--space-sm)">Uninstall</button>' : '';
        const cards = coverflowWindow(AppState.bigBoxGames, AppState.bigBoxIndex);
        $('bigBoxStage').innerHTML = `<div class="coverflow-strip" data-coverflow-strip>${cards.map(({item,index,offset = 0}) => `<button class="coverflow-card ${index === AppState.bigBoxIndex ? 'active' : 'jewel-case'}" data-coverflow="${index}" style="--coverflow-offset:${offset}" aria-label="Open ${escapeHtml(item.name)}">${index === AppState.bigBoxIndex ? renderCoverModel(item, { tools: false }) : `<img${item.has_cover ? '' : ' class="cover-placeholder"'} src="${coverSource(item)}" alt="" loading="lazy" decoding="async">`}</button>`).join('')}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${ownedLabel}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${canAct ? '' : 'disabled'}>${playLabel}</button>${steamRepairBtn}${uninstallBtn}</div>`;
        document.querySelector('[data-coverflow-strip]')?.addEventListener('click', event => {
          const button = event.target.closest('[data-coverflow]');
          if (!button) return;
          AppState.bigBoxIndex = Number(button.dataset.coverflow);
          AppState.selectedId = AppState.bigBoxGames[AppState.bigBoxIndex].id;
          renderBigBox();
        });
        bindCoverModel($('bigBoxStage'));
      } else {
        $('bigBoxStage').className = 'bigbox-stage';
        const model = coverModelFor(game) !== 'none' ? renderCoverModel(game) : '';
        $('bigBoxStage').innerHTML = `<div class="bigbox-cover${game.has_cover ? '' : ' no-image'}">${model || `<img${game.has_cover ? '' : ' class="cover-placeholder"'} src="${coverSource(game)}" alt="">`}</div><div class="bigbox-copy"><div class="hero-kicker">${escapeHtml(game.platform || '')}${game.gameyfin_id ? ` · ${game.store_installed ? 'Installed' : 'Owned'}` : ''}</div><h2>${escapeHtml(game.name)}</h2><p>${escapeHtml(game.description || [game.genre,game.developer].filter(Boolean).join(' · '))}</p><button class="bigbox-play" id="bigBoxPlay" ${(game.path_exists && game.store_installed !== false) || (game.gameyfin_id && !game.store_installed) || game.steam_installable ? '' : 'disabled'}>${playLabel}</button>${steamRepairBtn}${game.gameyfin_id && game.store_installed ? '<button class="icon-button" id="bigBoxUninstall" style="margin-left:var(--space-sm)">Uninstall</button>' : ''}</div>`;
        bindCoverModel($('bigBoxStage'));
        scheduleVideoSnap(game);
      }
      $('bigBoxPlay').onclick = () => activateCurrentGame(game);
      if ($('bigBoxDnaSimilar')) $('bigBoxDnaSimilar').onclick = () => dnaMoreLikeThisBigBox(game.name);
      if ($('bigBoxSteamRepair')) $('bigBoxSteamRepair').onclick = async () => {
        try {
          await api('/api/game/store-action', { method: 'POST', body: JSON.stringify({ id: game.id, game_id: game.game_id, action: 'repair' }) });
          notify(t('notify.app.steam_verify_queued'));
        } catch (error) { notify(error.message); }
      };
      if ($('bigBoxUninstall')) $('bigBoxUninstall').onclick = () => uninstallGameyfin(game);
      applyLibraryMusic();
    }
    function applyLibraryMusic() {
      const path = AppState.appSettings.library_music;
      if (!path) { if (AppState.libraryBgm) { AppState.libraryBgm.pause(); AppState.libraryBgm = null; } return; }
      if (!AppState.libraryBgm) { AppState.libraryBgm = new Audio(); AppState.libraryBgm.loop = true; AppState.libraryBgm.volume = AppState.appSettings.video_bgm_mix ? 0.35 : 0.6; }
      if (AppState.libraryBgm.src !== location.origin + path && !AppState.libraryBgm.src.endsWith(path)) { AppState.libraryBgm.pause(); AppState.libraryBgm.src = path; }
      if ($('bigBox').hidden) { AppState.libraryBgm.pause(); return; }
      AppState.libraryBgm.play().catch(() => {});
    }
    function closeBigBoxPause() {
      $('bigBoxPause').hidden = true;
      AppState.bigBoxLastInput = performance.now();
    }
    function bigBoxPauseOpen() {
      const panel = $('bigBoxPause');
      return Boolean(panel && !panel.hidden);
    }
    function openBigBoxPause() {
      const session = AppState.runningGames[0];
      if (!session) return openSessions();
      const game = gameForSession(session);
      $('bigBoxPauseTitle').textContent = session.game;
      $('bigBoxPauseMeta').textContent = `${session.paused ? 'Paused' : 'Running'} · started ${String(session.started || '').replace('T',' ')}`;
      const achievementSources = Object.keys(game?.achievement_sources || {}).length;
      const highscoreGame = Boolean(game && (game.platform === 'Arcade' || game.rom_name));
      $('bigBoxPauseActions').innerHTML = `<button class="primary" data-pause-action="${session.launch_id}:${session.paused ? 'resume' : 'pause'}">${session.paused ? 'Resume' : 'Pause'}</button><button class="icon-button" data-pause-action="${session.launch_id}:stop">Exit game</button>${game ? '<button class="icon-button" id="pauseMoment">Capture moment</button><button class="icon-button" id="pauseClip">Clip it</button>' : ''}${(game?.documents || []).map((item,index) => `<button class="icon-button" data-pause-doc="${game.id}:${index}">Read ${escapeHtml(item.name)}</button>`).join('') || ''}${AppState.raConfigured || achievementSources ? `<button class="icon-button" id="pauseAchievements">Achievements</button>` : ''}${highscoreGame ? '<button class="icon-button" id="pauseHighscores">High scores</button><button class="icon-button" id="pauseExportHighscores">Export high scores</button><button class="icon-button" id="pauseImportHighscores">Import high scores</button>' : ''}`;
      $('bigBoxPauseHighscores').textContent = '';
      document.querySelectorAll('[data-pause-action]').forEach(button => button.onclick = async () => {
        const [launch_id,action] = button.dataset.pauseAction.split(':');
        // S33: an unguarded `await` in an async onclick. A failed "Exit game"
        // produced an unhandled rejection and left the overlay open with the
        // card still claiming "Running" -- the user had no way to tell the
        // request failed, and the game they were still paying attention to was
        // still running. `sessions.js:229-233` wraps the identical call.
        try {
          await api('/api/session/control',{method:'POST',body:JSON.stringify({launch_id,action})});
        } catch (error) {
          notify(error.message);
          return;
        }
        $('bigBoxPause').hidden = true;
        openBigBoxPause();
      });
      document.querySelectorAll('[data-pause-doc]').forEach(button => button.onclick = () => {
        const [gameId,index] = button.dataset.pauseDoc.split(':');
        const targetGame = AppState.games.find(item => item.id === Number(gameId));
        if (targetGame) openReader(targetGame,Number(index));
      });
      if ($('pauseMoment')) $('pauseMoment').onclick = () => captureMomentInteractive(game, { trigger: 'pause', launchId: session.launch_id });
      if ($('pauseClip')) $('pauseClip').onclick = async () => { try { await captureClip({ game, launchId: session.launch_id }); } catch (error) { notify(error.message); } };
      if ($('pauseAchievements')) $('pauseAchievements').onclick = () => {
        $('bigBoxPause').hidden = true;
        const dialog = $('bigBoxAchievementsDialog');
        renderAchievementTabs($('bigBoxAchievementsContent'), game || {});
        const close = $('closeBigBoxAchievements');
        if (close) close.onclick = () => dialog.close();
        if (!dialog.open) openDialog(dialog);
      };
      if ($('pauseHighscores')) $('pauseHighscores').onclick = async () => {
        const target = $('bigBoxPauseHighscores');
        try {
          const result = await api(`/api/v2/highscores/table?id=${game.id}`);
          target.innerHTML = result.parsed
            ? `<input type="search" id="bigBoxHighscoreSearch" placeholder="Search scores" aria-label="Search scores"><table><thead><tr><th>Rank</th><th>Score</th><th>Name</th></tr></thead><tbody>${result.scores.map(row => `<tr><td>${escapeHtml(String(row.rank ?? ''))}</td><td>${escapeHtml(String(row.score ?? ''))}</td><td>${escapeHtml(row.name || '')}</td></tr>`).join('')}</tbody></table>`
            : escapeHtml(result.message || 'Not parsed (install hi2txt).');
          const search = $('bigBoxHighscoreSearch');
          if (search) search.oninput = () => {
            const query = search.value.trim().toLowerCase();
            target.querySelectorAll('tbody tr').forEach(row => {
              row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
            });
          };
        } catch (error) {
          target.textContent = error.message;
        }
      };
      if ($('pauseExportHighscores')) $('pauseExportHighscores').onclick = async () => {
        try {
          const result = await api('/api/highscores/export',{method:'POST',body:JSON.stringify({id:game.id})});
          notify(t('notify.library.high_scores_exported', {count: result.files.length}));
        } catch (error) { notify(error.message); }
      };
      if ($('pauseImportHighscores')) $('pauseImportHighscores').onclick = async () => {
        const path = await nativePickFile('Choose a MAME high score file');
        if (!path) return;
        try {
          const result = await api('/api/highscores/import',{method:'POST',body:JSON.stringify({id:game.id,path})});
          notify(`${result.restored.length} high score file(s) imported`);
        } catch (error) { notify(error.message); }
      };
      $('bigBoxPause').hidden = false;
      // Give the gamepad something to activate: A clicks the focused button.
      $('bigBoxPauseActions').querySelector('button')?.focus();
    }
    function startScreenSaver() {
      const visible = filteredBigBoxGames();
      if (!visible.length) return;
      const game = visible[Math.floor(Math.random() * visible.length)];
      AppState.screenSaverGame = game;
      if (game.has_video) {
        $('screenSaverVideo').src = media(game, 'video');
      } else if (game.has_cover) {
        $('screenSaverVideo').src = coverSource(game);
      } else {
        $('screenSaverVideo').removeAttribute('src');
      }
      $('screenSaverTitle').textContent = game.name;
      $('screenSaverPlatform').textContent = game.platform || 'Game';
      if ($('screenSaverStats')) {
        const stats = screenSaverStats(game);
        $('screenSaverStats').textContent = stats;
        $('screenSaverStats').hidden = !stats;
      }
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
          await api('/api/favorite',{method:'POST',body:JSON.stringify({id:game.id,game_id:game.game_id})});
          await refresh();
          AppState.bigBoxGames = filteredBigBoxGames();
          if (!AppState.bigBoxGames.length) { closeBigBox(); notify(t('bigbox.empty_view_now_empty')); return; }
          AppState.bigBoxIndex = Math.max(0,AppState.bigBoxGames.findIndex(g => g.id === game.id));
          renderBigBox();
        } catch(error) { notify(error.message); }
      }
    }
    // Gamepad surface handlers for the unified loop (gamepad.js): each tick the
    // loop dispatches to the highest-priority active surface, so these handle
    // one frame of input each and never schedule frames themselves.
    function firstGamepad() {
      const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
      return pads[0];
    }
    function bigBoxPadState(pad) {
      const mapping = {...defaultControllerMap,...AppState.appSettings.controller_map};
      const pressed = action => Boolean(pad.buttons[mapping[action]]?.pressed);
      return {left:pad.buttons[14]?.pressed || pad.axes[0] < -.6,right:pad.buttons[15]?.pressed || pad.axes[0] > .6,up:pad.buttons[12]?.pressed || pad.axes[1] < -.6,down:pad.buttons[13]?.pressed || pad.axes[1] > .6,play:pressed('play'),back:pressed('back'),favorite:pressed('favorite'),random:pressed('random'),pageLeft:pressed('page_left'),pageRight:pressed('page_right'),pause:pressed('pause'),menu:pressed('menu')};
    }
    function focusPauseButton(buttons, offset) {
      if (!buttons.length) return;
      let index = buttons.indexOf(document.activeElement);
      if (index < 0) index = offset > 0 ? 0 : buttons.length - 1;
      else index = (index + offset + buttons.length) % buttons.length;
      buttons[index].focus();
    }
    function pollBigBoxPause() {
      if (document.hidden || !document.hasFocus()) {
        stopGamepadLoop();
        return;
      }
      const pad = firstGamepad();
      if (!pad) return;
      const current = bigBoxPadState(pad);
      const edge = action => current[action] && !AppState.gamepadState[action];
      const edged = Object.fromEntries(Object.keys(current).map(action => [action, edge(action)]));
      const buttons = [...$('bigBoxPauseActions').querySelectorAll('button')].filter(button => !button.disabled);
      const action = firstEdge(edged, ['play', 'back']);
      if (!action) {
        if (edge('up') || edge('left')) focusPauseButton(buttons, -1);
        if (edge('down') || edge('right')) focusPauseButton(buttons, 1);
      } else if (action === 'play') {
        const focused = document.activeElement?.closest?.('#bigBoxPause button');
        (focused || buttons[0])?.click();
      } else if (action === 'back') {
        closeBigBoxPause();
      }
      if (Object.keys(current).some(edge)) AppState.bigBoxLastInput = performance.now();
      AppState.gamepadState = current;
    }
    function pollBigBoxGamepads() {
      if (document.hidden || !document.hasFocus()) {
        stopGamepadLoop();
        return;
      }
      const pad = firstGamepad();
      if (pad) {
        const current = bigBoxPadState(pad);
        const edge = action => current[action] && !AppState.gamepadState[action];
        if (AppState.gamepadState.back && !current.back) releaseBigBoxBack();
        if (bigBoxTypingActive() || document.querySelector('dialog[open]')) {
          AppState.gamepadState = current;
          return;
        }
        if (!$('screenSaver').hidden && Object.keys(current).some(edge)) {
          const game = AppState.screenSaverGame;
          stopScreenSaver();
          if (edge('play') && game) activateCurrentGame(game);
          AppState.gamepadState = current;
          return;
        }
        if (partyOverlayOpen()) {
          partyGamepad(edge);
        } else if (!$('bigBoxMenu').hidden) {
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
          const edged = Object.fromEntries(Object.keys(current).map(action => [action, edge(action)]));
          const action = firstEdge(edged, ACTION_ORDER);
          if (!action) {
            if (edge('left') || edge('up')) moveBigBox(-1);
            if (edge('right') || edge('down')) moveBigBox(1);
          } else if (action === 'play') {
            const target = AppState.bigBoxGames[AppState.bigBoxIndex];
            if (target) activateCurrentGame(target);
          } else if (action === 'back') {
            startBigBoxBack();
          } else if (action === 'favorite') {
            favoriteBigBox();
          } else if (action === 'random') {
            AppState.bigBoxIndex = Math.floor(Math.random() * AppState.bigBoxGames.length);
            renderBigBox();
          } else if (action === 'pageLeft') {
            moveBigBox(-10);
          } else if (action === 'pageRight') {
            moveBigBox(10);
          } else if (action === 'pause') {
            if (AppState.runningGames.length) openBigBoxPause();
          } else if (action === 'menu') {
            openBigBoxMenu();
          }
        }
        if (Object.keys(current).some(edge)) AppState.bigBoxLastInput = performance.now();
        AppState.gamepadState = current;
      }
      // Attract mode must not cover an open overlay: the pause panel and the
      // Party game-night overlay both own the screen while visible.
      // (Port: this guard was dropped by a461edd; restored.)
      if ($('screenSaver').hidden && $('bigBoxMenu').hidden && $('bigBoxPause').hidden && !partyOverlayOpen() && (() => {
        const delay = Number(AppState.appSettings.attract_mode_seconds ?? AppState.appSettings.screensaver_seconds ?? 0);
        return delay && performance.now() - AppState.bigBoxLastInput >= delay * 1000;
      })()) startScreenSaver();
    }

    registerGamepadSurface({ priority: 10, isActive: bigBoxPauseOpen, tick: pollBigBoxPause });
    registerGamepadSurface({ priority: 30, isActive: () => !$('bigBox')?.hidden, tick: pollBigBoxGamepads, wantsLoop: () => !$('bigBox')?.hidden });

    if (typeof document !== 'undefined') {
      window.addEventListener('openbox:theme-changed', event => {
        if (!$('bigBox') || $('bigBox').hidden) return;
        configureThemeSounds(event.detail?.theme || AppState.appSettings.theme, {
          enabled: AppState.appSettings.bigbox_sounds_enabled !== false,
        }).then(() => startThemeMusic()).catch(() => {});
      });
      window.addEventListener('pagehide', () => { pointerNavigator?.destroy(); pointerNavigator = null; });
      // (visibilitychange/gamepad lifecycle is owned by the unified loop in gamepad.js)
      $('bigBoxHybridSearch')?.addEventListener('focus', () => { AppState.bigBoxSearchMode = true; });
      $('partyMenuButton')?.addEventListener('click', () => { closeBigBoxMenu(); openParty(); });      $('bigBoxHybridSearch')?.addEventListener('blur', () => { AppState.bigBoxSearchMode = false; });
      $('bigBoxStartupVideo')?.addEventListener('ended', () => {
        const video = $('bigBoxStartupVideo');
        if (!video) return;
        video.classList.add('startup-video-done');
        setTimeout(() => { if (video.classList.contains('startup-video-done')) video.hidden = true; }, 300);
      });
      $('bigBoxHybridSearch')?.addEventListener('input', event => {
        AppState.bigBoxHybridQuery = event.target.value.trim();
        if (dnaSearchMode() === 'smart' && AppState.bigBoxHybridQuery) {
          const filters = AppState.bigBoxPlatform !== 'all' ? { platform: AppState.bigBoxPlatform } : {};
          scheduleBigBoxSmartSearch(AppState.bigBoxHybridQuery, filters, (error, payload) => {
            if (!$('bigBox') || $('bigBox').hidden) return;
            if (error) { if ($('bigBoxStatus')) $('bigBoxStatus').textContent = error.message; return; }
            applyBigBoxDnaResults(payload);
          });
          return;
        }
        cancelBigBoxSmartSearch();
        AppState.games.forEach(item => { delete item._dnaWhy; });
        AppState.bigBoxGames = filteredBigBoxGames();
        AppState.bigBoxIndex = 0;
        renderBigBox();
      });
      $('bigBoxDnaToggle')?.addEventListener('click', () => {
        setDnaSearchMode(dnaSearchMode() === 'smart' ? 'title' : 'smart', { search: false });
        const input = $('bigBoxHybridSearch');
        if (input && !input.hidden) input.dispatchEvent(new Event('input', { bubbles: true }));
        else renderBigBox();
      });
    }

export { openBigBox, closeBigBox, filteredBigBoxGames, openBigBoxMenu, closeBigBoxMenu, applyBigBoxMenu, moveBigBox, renderBigBox, applyLibraryMusic, openBigBoxPause, closeBigBoxPause, bigBoxPauseOpen, startScreenSaver, stopScreenSaver, favoriteBigBox, activateCurrentGame, bigBoxTypingActive, startBigBoxBack, releaseBigBoxBack, syncPointerNavigator };
