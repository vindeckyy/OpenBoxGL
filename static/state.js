import { stableIdFor } from './pure.js';
import { escapeHtml, API_V1, badge, defaultBadges, sortGames, advancedQueryMatches, parseQueryTokens, gameInstalled, $, motionMs, HTTP_URL_RE } from './util.js';
const tr = key => (window.OpenBoxI18n?.t ? window.OpenBoxI18n.t(key) : key);



// The launcher's ?token= param is scrubbed from the URL on load; keep it in
// sessionStorage so reloads stay authenticated for the tab session.
const token = new URLSearchParams(location.search).get('token') || sessionStorage.getItem('openbox.token') || '';
// First paint: apply the last known theme before the API answers, so a light theme does not open dark.
// (The real selection still arrives via loadTheme() and corrects this if it changed.)
try {
  const bootTheme = localStorage.getItem('openbox.theme');
  const bootLink = document.getElementById('themeStylesheet');
  if (bootTheme && bootLink && !bootLink.getAttribute('href')) bootLink.href = `/api/theme.css?name=${encodeURIComponent(bootTheme)}&token=${encodeURIComponent(token)}`;
} catch {}
    /**
     * Central client application state container.
     * @type {Record<string, any>}
     */
    const AppState = {
      games: [], playlists: [], filterPresets: [], explorerField: 'genre', explorerRules: {}, activeFilterPreset: '', bigBoxGames: [], runningGames: [], raConfigured: false, selectedId: null, platform: 'all', activePlaylist: '', editingId: null, metadataGameId: null, bigBoxIndex: 0, gamepadState: {}, lastSessionEvent: 0, bulkMode: false, bigBoxLastInput: performance.now(), screenSaverGame: null, contextGameId: null, availableProfiles: {},
      appSettings: {watch_folders:[],screensaver_seconds:90,controller_map:{},library_view:'grid',cover_grouping:'shape',locale:'en'}, bigBoxFilter: 'all', bigBoxSort: 'title', bigBoxRaFilter: 'all', bigBoxPlatform: 'all', platformCategory: 'all', pendingUpdate: null, duplicateMediaGroups: 0, libraryBgm: null, readerPage: 1, readerUrl: '', bigBoxHybridQuery: '', mediaEpoch: 0, coverRatios: {}, importBatchId: '', queryParse: null, queryParseText: '', queryMatchIds: null,
    };

    const selectedIds = new Set();
    const media = (game, kind, index = '') => `/api/media?id=${game.id}&kind=${kind}${index === '' ? '' : `&index=${index}`}&v=${AppState.mediaEpoch}&token=${encodeURIComponent(token)}`;
    const badgeVisibility = () => new Set(AppState.appSettings.badge_visibility || defaultBadges);
    const playlistFor = name => AppState.playlists.find(item => item.name === name);
    const playlistMembers = playlist => new Set((playlist?.members || []).map(String));
    // The stable id for a row's array index. Sent alongside the index so the server can
    // refuse a stale request instead of applying it to whichever game shifted into place.
    const gameIdOf = index => stableIdFor(AppState.games, index);
    const gameInPlaylist = (game, playlist) => {
      if (!playlist) return false;
      if (playlist.type !== 'manual') return true;
      return playlistMembers(playlist).has(String(game.game_id)) || playlistMembers(playlist).has(String(game.id));
    };
    function renderBadges(game) {
      const visible = badgeVisibility();
      return [
        game.manual_entry && badge('Shelf entry', true, 'shelf'),
        visible.has('favorite') && badge('Favorite', game.favorite, 'favorite'),
        visible.has('installed') && badge(gameInstalled(game) ? 'Installed' : 'Owned', true),
        visible.has('missing_media') && badge('Missing media', game.has_missing_media, 'danger'),
        visible.has('saves') && badge('Saves', game.has_saves),
        visible.has('documents') && badge('Docs', game.has_documents),
        visible.has('versions') && badge('Versions', game.has_versions),
        visible.has('storefront') && badge(game.source || 'Storefront', Boolean(game.source)),
        // [F1d importer plugins] source tag for plugin-imported games.
        game.plugin_source && badge(game.plugin_source_name || game.plugin_source, true, 'source'),
        visible.has('achievements') && badge('Achievements', game.has_achievements),
        visible.has('highscores') && badge('High scores', game.has_highscores),
        visible.has('progress') && badge(game.progress || 'Unplayed', true, 'progress'),
        visible.has('rating') && badge(`${game.rating} stars`, Number(game.rating) > 0),
        visible.has('user_rating') && badge(`${'\u2605'.repeat(Number(game.user_rating) || 0)}`, Number(game.user_rating) > 0, 'user-rating'),
        visible.has('manual') && badge('Manual time', Number(game.manual_playtime_seconds) > 0, 'manual-time'),
        visible.has('broken') && badge('Broken', game.broken, 'danger'),
        visible.has('portable') && badge('Portable', game.portable),
        visible.has('controller') && badge(game.controller_support, Boolean(game.controller_support)),
        // Launch readiness from the cached audit; absent when the audit is missing or stale, or the setting is off.
        AppState.appSettings?.show_launch_badges !== false && AppState.launchReadiness?.[game.game_id] === 'blocked' && badge(tr('launch_audit.badge_blocked'), true, 'danger'),
        AppState.appSettings?.show_launch_badges !== false && AppState.launchReadiness?.[game.game_id] === 'warning' && badge(tr('launch_audit.badge_warning'), true),
      ].filter(Boolean).join('');
    }
    /**
     * Perform an authenticated API request to the OpenBox backend.
     * @param {string} path
     * @param {RequestInit} [options]
     * @returns {Promise<any>}
     */
    // F9: no timeout and no signal pass-through. A server that stops responding
    // without closing the socket left `await api(...)` pending forever -- the
    // confirm dialog had already closed, no toast appeared, and the user had no
    // indication anything was still in flight. The default is generous because
    // a 20k-library import or a metadata scan is legitimately slow; callers that
    // can be superseded pass their own `signal` (F5's details pane does).
    const API_TIMEOUT_MS = 60000;
    async function api(path, options = {}) {
      // The v1 surface is the stable contract; unmapped call sites keep the
      // legacy paths until they are migrated one by one.
      const target = API_V1[path.replace(/^\/api\//, '').replace(/\//g, '_')] || path;
      const method = String(options.method || 'GET').toUpperCase();
      if (pageHidden && method !== 'GET' && method !== 'HEAD' && path !== '/api/shutdown') {
        // A hidden tab holds stale AppState; never let it save over newer
        // state written by the visible tab. /api/shutdown is exempt: it fires
        // from beforeunload while the page is already hidden.
        throw new Error('State changes are paused while the tab is hidden.');
      }
      const {signal, timeout = API_TIMEOUT_MS, ...rest} = options;
      const controller = new AbortController();
      // One signal, either from the caller or from the deadline -- not two racing
      // controllers, or a caller-initiated abort would leave the timer running.
      const abort = () => controller.abort(signal?.reason);
      if (signal) {
        if (signal.aborted) abort();
        else signal.addEventListener('abort', abort, {once: true});
      }
      const timer = timeout > 0 && Number.isFinite(timeout) ? setTimeout(abort, timeout) : null;
      let response;
      try {
        response = await fetch(target, { ...rest, signal: controller.signal, headers:{'X-OpenBox-Token':token,'Content-Type':'application/json',...(rest.headers || {})} });
      } catch (error) {
        if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        if (controller.signal.aborted) throw new Error(`The server did not respond within ${Math.round(timeout / 1000)}s.`);
        throw new Error(error.message || 'Could not reach the OpenBox server.');
      } finally {
        if (timer) clearTimeout(timer);
        if (signal) signal.removeEventListener('abort', abort);
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
    // Page lifecycle: a hidden tab must not save stale AppState over the
    // visible tab, and must not hold SSE streams open (the server reaps them
    // only via the 15 s heartbeat). pageHidden gates state-mutating api()
    // calls above; the stream registry lets SSE owners close on pagehide.
    let pageHidden = document.visibilityState === 'hidden';
    const lifecycleStreams = new Set();
    function isPageHidden() { return pageHidden; }
    function registerLifecycleStream(source) {
      if (source && typeof source.close === 'function') lifecycleStreams.add(source);
      return source;
    }
    function unregisterLifecycleStream(source) { lifecycleStreams.delete(source); }
    function closeLifecycleStreams() {
      for (const source of lifecycleStreams) {
        try { source.close(); } catch { /* already closed */ }
      }
      lifecycleStreams.clear();
    }
    function markPageHidden() {
      pageHidden = true;
      closeLifecycleStreams();
    }
    function markPageVisible() { pageHidden = false; }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) markPageHidden();
      else markPageVisible();
    });
    // pagehide also fires for bfcache navigations where visibilitychange may
    // not; pageshow re-arms the tab if it comes back.
    window.addEventListener('pagehide', markPageHidden);
    window.addEventListener('pageshow', markPageVisible);
    const nativeBridge = typeof window.openboxNative === 'object' ? window.openboxNative : null;
    const nativeCaps = { webview:false, dialogs:false, tray:false, single_instance:false, gamepad:'webkit', fullscreen:true, clipboard:true };
    async function detectNative() {
      try {
        Object.assign(nativeCaps, await api('/api/native/capabilities'));
      } catch { /* no host connected; browser fallbacks stay active */ }
    }
    function nativeEnabled(feature) { return Boolean(nativeCaps[feature]); }
    function nativePrompt(message, defaultValue = '') {
      return import('./dialogs.js').then(({ promptInput }) => promptInput({ message, defaultValue }));
    }
    async function nativeConfirm(message) {
      const { confirmAction } = await import('./dialogs.js');
      return await confirmAction({ message, target: message });
    }
    async function nativePickFolder(message) {
      if (nativeBridge?.dialog) {
        try {
          const result = await nativeBridge.dialog('folder', {title:message});
          return result?.path || null;
        } catch { return null; }
      }
      const { promptInput } = await import('./dialogs.js');
      return await promptInput({ message, label: 'Folder path', defaultValue: '' });
    }
    async function nativePickFile(message) {
      if (nativeBridge?.dialog) {
        try {
          const result = await nativeBridge.dialog('file', {title:message});
          return result?.path || null;
        } catch { return null; }
      }
      const { promptInput } = await import('./dialogs.js');
      return await promptInput({ message, label: 'File path', defaultValue: '' });
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
    // F2: this is the one place an external URL is *navigated* rather than
    // rendered, and the only branch that takes the value verbatim. The native
    // hosts both validate (`uri_scheme_allowed`), but the browser-served
    // deployment -- `nativeCaps.dialogs === false`, and also any host-detect
    // failure, which is `.catch(() => {})`-swallowed -- falls through to
    // `window.open`, and `wikipedia_url` is free text from `index.html:151` with
    // no `type="url"` and no `pattern`. So `javascript:` reached window.open
    // unvalidated. One rule, applied at the top, covers all three branches and
    // keeps this consistent with `fact()` and `gameLinks()` in util.js.
    // S28: a running session carries two identifiers and every consumer reached
    // for the wrong one. `game_id` is the game's position in the `games` array;
    // `item.id` is the library row id. They agree only until the library is
    // re-sorted, re-imported, or has a row removed -- and then a session card
    // renders the extras of a *different* game ("Read ⟨wrong manual⟩"), the
    // "Back up saves" button targets the wrong game, and Big Box attributes the
    // moment to the wrong title. `stable_game_id` is the identifier that survives
    // all three, and the one every other consumer already uses.
    //
    // The index is still accepted as a last resort, because a session persisted
    // before stable ids were recorded carries nothing else -- and an approximate
    // match beats no match when a game is genuinely running.
    function gameForSession(session) {
      if (!session) return null;
      const stable = String(session.stable_game_id || '').trim();
      if (stable) {
        const byStable = AppState.games.find(item => String(item.game_id) === stable);
        if (byStable) return byStable;
      }
      const legacy = session.game_id;
      if (legacy !== undefined && legacy !== null) {
        const byRowId = AppState.games.find(item => item.id === legacy);
        if (byRowId) return byRowId;
        const byIndex = AppState.games[Number(legacy)];
        if (byIndex) return byIndex;
      }
      // Some payloads already use `game_id` for the stable id (session.started
      // does, and navigation.js relies on it); try that reading last so a legacy
      // numeric index still wins when it resolves.
      const asStable = String(legacy || '').trim();
      if (asStable && !/^\d+$/.test(asStable)) {
        const byAlternate = AppState.games.find(item => String(item.game_id) === asStable);
        if (byAlternate) return byAlternate;
      }
      return null;
    }
    async function nativeOpenExternal(target) {
      const url = String(target || '').trim();
      if (!HTTP_URL_RE.test(url)) return false;
      if (nativeBridge?.openExternal) { const result = await nativeBridge.openExternal(url); return result?.ok; }
      if (nativeEnabled('dialogs')) { const result = await api('/api/native/open-external',{method:'POST',body:JSON.stringify({url})}); return result?.ok; }
      window.open(url, '_blank', 'noopener,noreferrer');
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
    const NOTIFY_LEVELS = new Set(['success', 'info', 'warning', 'error']);
    function notify(levelOrMessage, message, options) {
      let level, text, opts;
      if (arguments.length === 1 && NOTIFY_LEVELS.has(levelOrMessage)) {
        level = levelOrMessage;
        text = '';
        opts = {};
      } else if (arguments.length === 1) {
        level = 'info';
        text = String(levelOrMessage ?? '');
        opts = {};
      } else {
        level = levelOrMessage;
        text = String(message ?? '');
        opts = options || {};
      }
      showToast({ level, text, ms: opts.ms, action: opts.action, onAction: opts.onAction });
      if (level === 'error' && opts.actionable) {
        const banner = $('errorBanner');
        if (banner) {
          $('errorBannerText').textContent = text;
          lastBannerDetails = [text, opts.code ? `code: ${opts.code}` : '', opts.requestId ? `request id: ${opts.requestId}` : '', opts.detail ? String(opts.detail) : ''].filter(Boolean).join('\n');
          banner.hidden = false;
          clearTimeout(showErrorBanner.timer);
        }
      }
    }

    // ── Toast manager ────────────────────────────────────────────────────────
    //
    // 1.15.0 claimed "one queue-safe surface in the top layer". There was no
    // queue: four writers targeted a single #toast element and three of them
    // assigned its innerHTML outright. Delete a game, get an Undo button for 8
    // seconds, let a trophy unlock, and the innerHTML assignment removed the
    // button -- the deleted game could no longer be undone from the UI. The
    // one guarded writer held a single `pending` slot, so a third message
    // silently overwrote the queued second.
    //
    // Toasts are now independent entries in a capped stack. Nothing writes
    // another toast's DOM, so an Undo button survives every other message.

    const TOAST_LIMIT = 3;
    const TOAST_DEFAULT_MS = 2800;
    const TOAST_ACTION_MS = 8000;
    const toastEntries = new Map();   // id -> entry
    let toastSequence = 0;

    function popoverOpen(el) { try { return el.matches(':popover-open'); } catch { return false; } }

    // Exit through one function, so every toast leaves the way it arrived.
    // 1.15.0 specified this and never wrote it; each writer had rolled its own
    // class-toggle instead, which is how three of them diverged.
    function leave(el, done) {
      el.classList.add('leaving');
      const ms = motionMs('--dur-out');
      if (ms <= 1) { done(); return; }
      let finished = false;
      const onEnd = event => { if (event.target === el) finish(); };
      function finish() {
        if (finished) return;
        finished = true;
        el.removeEventListener('animationend', onEnd);
        done();
      }
      el.addEventListener('animationend', onEnd);
      setTimeout(finish, ms + 60);
    }

    function armTimer(entry) {
      clearTimeout(entry.timer);
      entry.startedAt = Date.now();
      entry.remaining = entry.ms;
      entry.timer = setTimeout(() => dismissToast(entry.id), entry.ms);
    }

    // B3: the pause must be a real pause. Re-arming for the full duration would
    // still fade the toast out from under a cursor resting on Undo, which is
    // the WCAG 2.2.1 complaint (timing adjustable) restated as a bug.
    function pauseTimer(entry) {
      if (entry.paused) return;
      entry.paused = true;
      clearTimeout(entry.timer);
      entry.remaining = Math.max(0, entry.ms - (Date.now() - entry.startedAt));
    }

    function resumeTimer(entry) {
      if (!entry.paused) return;
      entry.paused = false;
      // Never re-arm with zero: a toast that was paused at the very end should
      // still be reachable for a moment.
      entry.timer = setTimeout(() => dismissToast(entry.id), Math.max(entry.remaining, 1200));
    }

    // Stack the toasts upward from the container's bottom edge with an explicit
    // offset per toast. A flex column would grow the container as items are
    // appended and slide every toast already on screen -- measured at 0.0017 CLS
    // each time a second message arrived. With the offsets assigned here, a new
    // toast appears without moving the others.
    const TOAST_GAP = 8;
    function restackToasts() {
      const elements = [...toastEntries.values()].map(entry => entry.el);
      let offset = 0;
      for (let index = elements.length - 1; index >= 0; index--) {
        const el = elements[index];
        el.style.bottom = `${offset}px`;
        offset += el.offsetHeight + TOAST_GAP;
      }
    }

    function dismissToast(id) {
      const entry = toastEntries.get(id);
      if (!entry) return;
      clearTimeout(entry.timer);
      toastEntries.delete(id);
      leave(entry.el, () => {
        entry.el.remove();
        if (!toastEntries.size) {
          const layer = $('toasts');
          try { if (layer && popoverOpen(layer)) layer.hidePopover(); } catch {}
        } else {
          restackToasts();
        }
      });
    }

    function showToast({ level = 'info', text = '', action = null, ms = null, onAction = null }) {
      const layer = $('toasts');
      if (!layer) return null;
      const id = ++toastSequence;
      const el = document.createElement('div');
      el.className = 'toast';
      el.id = `toast-${id}`;
      el.dataset.notifyLevel = level;
      // The live region below is the accessible announcement; this is the
      // visual element, and role=status here would announce it twice.
      const body = document.createElement('span');
      body.className = 'toast-text';
      body.textContent = text;
      el.appendChild(body);
      if (action) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'toast-action';
        button.textContent = action;
        button.addEventListener('click', () => {
          dismissToast(id);
          if (typeof onAction === 'function') onAction();
        });
        el.appendChild(button);
      }
      // The *container* is the top-layer element, not each toast: popovers are
      // taken out of normal flow, so per-toast popovers would all anchor to the
      // same corner instead of stacking.
      if (typeof layer.showPopover === 'function' && !popoverOpen(layer)) {
        try { layer.showPopover(); } catch {}
      }
      layer.appendChild(el);
      void el.offsetWidth;   // start the enter transition from the hidden state
      el.classList.add('show');

      const live = $('toastLive');
      if (live) live.textContent = text;

      const entry = {
        id, el, level, text,
        ms: ms ?? (action ? TOAST_ACTION_MS : TOAST_DEFAULT_MS),
        timer: 0, startedAt: 0, remaining: 0, paused: false,
      };
      toastEntries.set(id, entry);

      // Hover and focus both pause. Focus matters more -- WCAG 2.2.1 is about
      // the focused element, and a timer that removes a focused button drops
      // focus to <body>.
      el.addEventListener('mouseenter', () => pauseTimer(entry));
      el.addEventListener('mouseleave', () => resumeTimer(entry));
      el.addEventListener('focusin', () => pauseTimer(entry));
      el.addEventListener('focusout', event => {
        if (el.contains(event.relatedTarget)) return;
        resumeTimer(entry);
      });

      armTimer(entry);
      enforceLimit();
      // After the new toast is in the DOM, so offsetHeight is real.
      restackToasts();
      return id;
    }

    function enforceLimit() {
      // Oldest first: the newest message is the one the user just triggered.
      const ids = [...toastEntries.keys()];
      for (const id of ids.slice(0, Math.max(0, ids.length - TOAST_LIMIT))) dismissToast(id);
    }

    // B5: top-layer elements are ordered by when they were shown, so a toast
    // container opened before a <dialog> sits underneath it -- and every dialog
    // here is a native one, so ::backdrop covered the Undo button for the rest
    // of its life. Re-showing moves a popover to the top of the top layer, so
    // the dialog opener calls this after showing itself.
    function raiseToasts() {
      const layer = $('toasts');
      if (!layer || !toastEntries.size) return;
      if (typeof layer.showPopover !== 'function' || popoverOpen(layer)) return;
      try { layer.showPopover(); } catch {}
    }

    // Compatibility surface for the three migrated writers and for callers
    // outside static/. hideToast() dismisses the newest toast, which is the
    // one a writer was talking about.
    function revealToast(ms) {
      const ids = [...toastEntries.keys()];
      const newest = ids.at(-1);
      if (newest == null) return;
      const entry = toastEntries.get(newest);
      if (ms) { entry.ms = ms; armTimer(entry); }
    }
    function hideToast() {
      const ids = [...toastEntries.keys()];
      const newest = ids.at(-1);
      if (newest != null) dismissToast(newest);
    }
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
      if (!error?.actionable) {
        clearTimeout(showErrorBanner.timer);
        showErrorBanner.timer = setTimeout(() => { $('errorBanner').hidden = true; }, 10000);
      } else {
        clearTimeout(showErrorBanner.timer);
      }
    }
    function copyDiagnostics() {
      try {
        navigator.clipboard.writeText(lastBannerDetails || 'No error details captured.');
        notify(tr('notify.state.error_copied'));
      } catch (error) {
        notify(tr('notify.state.copy_failed'));
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
    const INDEX_PREFIX_MAX = 9;
    function indexedTitleCandidates(query) {
      if (!query || /[:"]/.test(query)) return null;
      const tokens = cachedParseQueryTokens(query);
      if (!tokens.length || tokens.some(token => token.negative || token.key !== 'title' || token.value.length < 2 || !/^[a-z0-9]+$/.test(token.value))) return null;
      if (tokens.some(token => token.value.length > INDEX_PREFIX_MAX)) return null;
      const index = buildSearchIndex();
      let ids = null;
      for (const token of tokens) {
        const prefixKey = token.value.slice(0, SEARCH_INDEX_MAX_TERM);
        if (!index.title.has(prefixKey)) return null;
        const bucket = index.title.get(prefixKey) || [];
        const next = new Set(bucket.map(game => game.id));
        ids = ids === null ? next : new Set([...ids].filter(id => next.has(id)));
        if (!ids.size) break;
      }
      return ids.size ? index.all.filter(game => ids.has(game.id)) : [];
    }
    function warmSearchIndex() {
      buildSearchIndex();
    }
    function resetQuery() {
      if ($('sidebarSearch')) $('sidebarSearch').value = '';
      if ($('view')) $('view').value = 'all';
      AppState.platform = 'all';
      AppState.platformCategory = 'all';
      if ($('esrbFilter')) $('esrbFilter').value = '';
      AppState.activePlaylist = '';
      AppState.activeFilterPreset = '';
      AppState.explorerRules = {};
      AppState.importBatchId = '';
      AppState.queryParse = null;
      AppState.queryParseText = '';
      AppState.queryMatchIds = null;
      invalidateFilterCache();
    }
    function resolveDeeplinkGameId(id) {
      if (!id) return null;
      const stable = AppState.games.find(game => String(game.game_id) === String(id));
      if (stable) return stable.id;
      const index = Number(id);
      if (Number.isInteger(index) && index >= 0 && index < AppState.games.length) return AppState.games[index]?.id ?? null;
      return null;
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
      const sortDir = AppState.appSettings.list_sort_dir || '';
      const esrb = $('esrbFilter')?.value || '';
      const key = `${_filterVersion}\0${AppState._refreshCounter || 0}\0${query}\0${view}\0${sort}\0${sortDir}\0${esrb}\0${AppState.platform}\0${AppState.platformCategory}\0${AppState.activePlaylist}\0${AppState.activeFilterPreset}\0${AppState.importBatchId}\0${JSON.stringify(AppState.explorerRules)}`;
      if (_filteredCache.key === key) return _filteredCache.result;
      const preset = AppState.filterPresets.find(item => item.name === AppState.activeFilterPreset);
      const presetRules = preset?.rules || {};
      const activePlaylistData = playlistFor(AppState.activePlaylist);
      const parsedQueryText = String(AppState.queryParseText || '').toLowerCase().trim();
      const smartQueryActive = !presetRules.query && parsedQueryText === query && AppState.queryMatchIds instanceof Set;
      const indexQuery = smartQueryActive ? '' : (presetRules.query || query).trim();
      const sourceGames = smartQueryActive ? AppState.games : (indexedTitleCandidates(indexQuery) || AppState.games);
      const visible = sourceGames.filter(game => {
        const completed = ['Beaten','Completed','Mastered'].includes(game.progress);
        const installed = gameInstalled(game);
        const ownedUninstalled = Boolean(game.owned || game.store_catalog || game.gameyfin_id) && !installed;
        const effectiveView = presetRules.view || view;
        const viewMatch = (effectiveView === 'all' && !game.hidden) || (effectiveView === 'favorites' && game.favorite && !game.hidden) || (effectiveView === 'recent' && game.last_played && !game.hidden) || (effectiveView === 'never' && !game.play_count && !game.hidden) || (effectiveView === 'playing' && ['Playing','Paused'].includes(game.progress) && !game.hidden) || (effectiveView === 'completed' && completed && !game.hidden) || (effectiveView === 'unplayed' && !game.progress && !game.hidden) || (effectiveView === 'installed' && installed && !game.hidden) || (effectiveView === 'owned' && ownedUninstalled && !game.hidden) || (effectiveView === 'saves' && game.has_saves && !game.hidden) || (effectiveView === 'shelf' && game.manual_entry && !game.hidden) || (effectiveView === 'hidden' && game.hidden) || (effectiveView === 'missing' && !game.path_exists && !game.manual_entry && !game.hidden);
        const effectivePlatform = presetRules.platform || AppState.platform;
        const platformMatch = effectivePlatform === 'all' || game.platform === effectivePlatform;
        const category = presetRules.platform_category || AppState.platformCategory;
        const categoryMatch = category === 'all' || platformCategoryFor(game) === category;
        const esrb = presetRules.esrb || $('esrbFilter')?.value || '';
        const esrbMatch = !esrb || (game.esrb || 'Unrated') === esrb;
        const effectiveQuery = (presetRules.query || query).trim();
        const queryMatch = !effectiveQuery
          ? true
          : smartQueryActive
            ? AppState.queryMatchIds.has(String(game.game_id || game.id || ''))
            : advancedQueryMatches(game, effectiveQuery);
        const batchMatch = !AppState.importBatchId || String(game.import_batch_id) === AppState.importBatchId;
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
        return viewMatch && platformMatch && categoryMatch && esrbMatch && queryMatch && batchMatch && progressMatch && explorerProgressMatch && favoriteMatch && genreMatch && developerMatch && publisherMatch && installedMatch && hiddenMatch && playlistMatch;
      });
      const sorted = sortGames(visible, sort, sortDir);
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
              if (button.dataset.explorerField === 'genre') $('sidebarSearch').value = `genre:"${button.dataset.explorerValue}"`;
              else if (button.dataset.explorerField === 'developer') $('sidebarSearch').value = `developer:"${button.dataset.explorerValue}"`;
              else if (button.dataset.explorerField === 'platform') AppState.platform = button.dataset.explorerValue;
              else if (button.dataset.explorerField === 'progress') AppState.explorerRules = {progress:button.dataset.explorerValue === 'Unplayed' || button.dataset.explorerValue === 'Unset' ? '__unset' : button.dataset.explorerValue};
              else if (button.dataset.explorerField === 'esrb' && $('esrbFilter')) $('esrbFilter').value = button.dataset.explorerValue === 'Unrated' ? 'Unrated' : button.dataset.explorerValue;
              // Lazy: a static import makes state.js and library.js a cycle whose evaluation order depends on the
              // engine, and library.js then touches AppState before this module has initialized it.
              import('./library.js').then(library => library.render());
            };
          } else {
            button.onclick = () => loadExplorerFacets(button.dataset.explorerField);
          }
        });
      } catch(error) {
        container.innerHTML = `<span class="description">${escapeHtml(error.message)}</span>`;
      }
    }

export { gameIdOf, nativeCaps, revealToast, hideToast, showToast, dismissToast, raiseToasts, token, AppState, selectedIds, media, badgeVisibility, playlistFor, playlistMembers, gameInPlaylist, renderBadges, api, gameForSession, nativeBridge, detectNative, nativeEnabled, nativePrompt, nativeConfirm, nativePickFolder, nativePickFile, nativeReveal, nativeOpenExternal, nativeWindowAction, nativeFullscreenOn, nativeFullscreen, notify, lastBannerDetails, showErrorBanner, copyDiagnostics, setButtonBusy, profilesFetched, ensureProfiles, applyLocaleStrings, applySidebarVisibility, platformCategoryFor, filteredGames, warmSearchIndex, loadExplorerFacets, invalidateFilterCache, markSearchIndexDirty, scheduleSearch, resetQuery, resolveDeeplinkGameId, isPageHidden, registerLifecycleStream, unregisterLifecycleStream };
