



    const defaultControllerMap = {play:0,back:1,favorite:2,random:3,page_left:4,page_right:5,pause:8,menu:9};

    /**
     * Shorthand for document.getElementById.
     * @param {string} id
     * @returns {HTMLElement | null}
     */
    const $ = id => document.getElementById(id);

    /**
     * Escape HTML special characters for safe markup interpolation.
     * @param {unknown} value
     * @returns {string}
     */
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

    /**
     * Format elapsed seconds into human-readable hours and minutes.
     * @param {number} seconds
     * @returns {string}
     */
    const duration = seconds => { const minutes = Math.floor((seconds || 0) / 60), hours = Math.floor(minutes / 60); return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`; };

    const defaultBadges = ['favorite','installed','saves','documents','progress','storefront','achievements','rating'];

    /**
     * Check if a game is considered installed on the local system or storefront.
     * @param {Record<string, any>} game
     * @returns {boolean}
     */
    const gameInstalled = game => game.store_installed !== false && (game.path_exists || game.store_installed);

    /**
     * Format bytes into human-readable KB or MB.
     * @param {number | string} value
     * @returns {string}
     */
    const formatBytes = value => {
      const bytes = Number(value || 0);
      if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
      if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
      if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      return `${Math.round(bytes)} B`;
    };

    /**
     * localStorage access that cannot throw in privacy mode or sandboxed
     * webviews. Render paths call this on every paint, so unguarded access
     * (SecurityError / quota) used to break rendering entirely.
     */
    const safeStorage = {
      get(key) { try { return localStorage.getItem(key); } catch { return null; } },
      set(key, value) { try { localStorage.setItem(key, String(value)); return true; } catch { return false; } },
      remove(key) { try { localStorage.removeItem(key); return true; } catch { return false; } },
    };

    /**
     * True when the user asked the OS to minimize motion. Gate any programmatic
     * scrolling, transition, or animation on this — CSS rules live in app.css.
     * @returns {boolean}
     */
    const prefersReducedMotion = () =>
      typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
        : false;

    /**
     * ARIA guidance for the virtualized game grid (P7).
     *
     * index.html already declares `#grid` as `role="grid"` with
     * `aria-rowcount`/`aria-colcount` placeholders. The rows and cells are
     * rendered by `static/library.js`, which has not adopted these helpers yet.
     * When it does:
     *   1. call `applyGridA11y($('grid'), {rowCount, colCount})` after geometry
     *      is computed, and
     *   2. render each card as `role="row"` and its `[data-game]` button as
     *      `role="gridcell"` with the attributes from `gridCellAttrs()`.
     * Rows are presentational wrappers; the keyboard target stays the button.
     * @param {HTMLElement | null | undefined} container
     * @param {{rowCount?: number, colCount?: number}} [geometry]
     */
    function applyGridA11y(container, { rowCount = -1, colCount = -1 } = {}) {
      if (!container) return;
      container.setAttribute('role', 'grid');
      if (rowCount >= 0) container.setAttribute('aria-rowcount', String(rowCount));
      if (colCount >= 0) container.setAttribute('aria-colcount', String(colCount));
    }

    /**
     * Build the role/position attributes for one virtualized grid cell.
     * `aria-rowindex`/`aria-colindex` stay 1-based and derive from the flat
     * cell index so virtual windows keep correct positions. `aria-setsize` /
     * `aria-posinset` are only emitted for list-style surfaces.
     * @param {{index?: number, columns?: number, rowCount?: number, colCount?: number, size?: number, position?: number, selected?: boolean}} [options]
     * @returns {string}
     */
    function gridCellAttrs({ index = 0, columns = 1, rowCount, colCount, size, position, selected = false } = {}) {
      const attrs = ['role="gridcell"'];
      if (columns > 0) {
        attrs.push(`aria-rowindex="${Math.floor(index / columns) + 1}"`);
        attrs.push(`aria-colindex="${(index % columns) + 1}"`);
      }
      if (rowCount != null) attrs.push(`aria-rowcount="${rowCount}"`);
      if (colCount != null) attrs.push(`aria-colcount="${colCount}"`);
      if (size != null) attrs.push(`aria-setsize="${size}"`);
      if (position != null) attrs.push(`aria-posinset="${position}"`);
      if (selected) attrs.push('aria-selected="true"');
      return attrs.join(' ');
    }

    const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/;
    let _dateFormatters = {};
    let _dateFormatterLang = null;
    function dateFormatter(hasTime) {
      const lang = (typeof document !== 'undefined' && document.documentElement.lang) || 'en';
      if (_dateFormatterLang !== lang) {
        _dateFormatterLang = lang;
        _dateFormatters = {};
      }
      const slot = hasTime ? 'datetime' : 'date';
      if (!_dateFormatters[slot]) {
        const options = hasTime ? { dateStyle: 'medium', timeStyle: 'short' } : { dateStyle: 'medium' };
        try { _dateFormatters[slot] = new Intl.DateTimeFormat(lang, options); }
        catch { _dateFormatters[slot] = null; }
      }
      return _dateFormatters[slot];
    }
    /**
     * Shared date rendering with Intl.DateTimeFormat. Handles ISO datetimes,
     * date-only strings (parsed as local dates, not UTC midnight), Date
     * objects, and garbage values (passed through with the T separator).
     * @param {string | number | Date | null | undefined} value
     * @returns {string}
     */
    function formatDate(value) {
      if (value === null || value === undefined || value === '') return '';
      const text = value instanceof Date ? null : String(value);
      const hasTime = text === null || !DATE_ONLY_RE.test(text);
      let date;
      if (value instanceof Date) date = value;
      else if (DATE_ONLY_RE.test(text)) {
        const [year, month, day] = text.split('-').map(Number);
        date = new Date(year, month - 1, day);
      } else date = new Date(text);
      if (Number.isNaN(date.getTime())) return text.replace('T', ' ');
      const formatter = dateFormatter(hasTime);
      return formatter ? formatter.format(date) : date.toLocaleString();
    }
    if (typeof document !== 'undefined') {
      // The active locale arrives asynchronously; drop the cached formatter so
      // the next render uses the new locale.
      document.addEventListener('localechange', () => { _dateFormatterLang = null; });
    }

    const queryTokenCache = new Map();

    /**
     * Parse an advanced search query string into structured key/value/negative tokens.
     * @param {string} query
     * @returns {Array<{negative: boolean, key: string, value: string}>}
     */
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
    /**
     * Match a game object against an advanced search query.
     * @param {Record<string, any>} game
     * @param {string} query
     * @returns {boolean}
     */
    function advancedQueryMatches(game, query) {
      const parsedTokens = parseQueryTokens(query);
      const fields = {
        title:['name','sort_title','alternate_names'], platform:['platform'], plat:['platform'], genre:['genre'],
        dev:['developer'], developer:['developer'], pub:['publisher'], publisher:['publisher'], series:['series'],
        region:['region'], play:['play_mode'], playmode:['play_mode'], notes:['notes'], source:['source'],
        store:['source'], storefront:['source'], status:['status'], progress:['progress'], rating:['rating'],
        favorite:['favorite'], fav:['favorite'], installed:['installed'], hide:['hidden'], hidden:['hidden'],
        broken:['broken'], portable:['portable'], controller:['controller_support'], tag:['tags'], tags:['tags'],
        import_batch_id:['import_batch_id'], import_batch:['import_batch_id'],
        all:['name','sort_title','alternate_names','platform','genre','developer','publisher','series','region','notes','source','play_mode','status','progress','controller_support','tags']
      };
      return parsedTokens.every(({ negative, key, value }) => {
        if (key === 'import_batch_id' || key === 'import_batch') {
          const matched = String(game.import_batch_id || '').toLowerCase() === value;
          return negative ? !matched : matched;
        }
        const names = fields[key] || fields.all;
        const values = names.flatMap(name => Array.isArray(game[name]) ? game[name] : [game[name]]).filter(item => item !== undefined && item !== null && item !== '');
        if (key === 'installed') values.push(gameInstalled(game) ? 'yes' : 'no');
        if (key === 'favorite' || key === 'fav') values.push(game.favorite ? 'yes' : 'no');
        if (key === 'hide' || key === 'hidden') values.push(game.hidden ? 'yes' : 'no');
        if (key === 'broken') values.push(game.broken ? 'yes' : 'no');
        if (key === 'portable') values.push(game.portable ? 'yes' : 'no');
        let matched = values.map(item => String(item).toLowerCase()).some(item => item.includes(value));
        if (!matched && key === 'all' && value.length >= 2 && value.length <= 8 && /^[a-z0-9]+$/i.test(value)) {
          const title = String(game.name || '').trim();
          const words = title.match(/[A-Za-z0-9]+/g) || [];
          const acronym = words.map(w => w[0].toLowerCase()).join('');
          if (acronym === value || acronym.includes(value)) matched = true;
          else if (words.length > 1 && ['the', 'a', 'an'].includes(words[0].toLowerCase())) {
            const subAcronym = words.slice(1).map(w => w[0].toLowerCase()).join('');
            if (subAcronym === value || subAcronym.includes(value)) matched = true;
          }
        }
        return negative ? !matched : matched;
      });
    }
    /**
     * Render an HTML badge tag if value is truthy.
     * @param {string} label
     * @param {unknown} value
     * @param {string} [kind]
     * @returns {string}
     */
    function badge(label, value, kind = '') { return value ? `<span class="badge ${kind}" title="${escapeHtml(label)}">${escapeHtml(label)}</span>` : ''; }
    const artworkKinds = [['clear_logo','Clear logo','has_clear_logo'],['fanart','Fanart','has_fanart'],['banner','Banner','has_banner'],['icon','Icon','has_icon'],['box_back','Box back','has_box_back'],['box_spine','Box spine','has_box_spine'],['box_3d','3D box','has_box_3d'],['title_screen','Title screen','has_title_screen'],['cart_front','Cart front','has_cart_front'],['cart_back','Cart back','has_cart_back'],['disc','Disc','has_disc'],['advertisement','Advertisement / flyer','has_advertisement'],['manual','Manual','has_manual']];
    const API_V1 = {
      library: '/api/v1/library', settings: '/api/v1/settings', health: '/api/v1/health',
      health_dedupe: '/api/v1/health/dedupe', launch: '/api/v1/launch',
      game: '/api/v1/game', game_delete: '/api/v1/game/delete',
      games_bulk: '/api/v1/games/bulk',
      'games_bulk-wizard': '/api/v1/games/bulk-wizard',
      queue: '/api/v1/queue', tags: '/api/v1/tags',
      notifications: '/api/v1/notifications', webhooks: '/api/v1/webhooks',
      playlists: '/api/v1/playlists',
      running: '/api/v1/running', history: '/api/v1/history',
      saves: '/api/v1/saves', saves_scan_apply: '/api/v1/saves/scan/apply',
      favorite: '/api/v1/favorite', shutdown: '/api/v1/shutdown',
      log: '/api/v1/log', diagnostic: '/api/v1/diagnostic',
      jobs: '/api/v1/jobs', state_recover: '/api/v1/state/recover',
      media: '/api/v1/media', media_bulk: '/api/v1/media/bulk',
      media_audit: '/api/v1/media/audit', media_cleanup: '/api/v1/media/cleanup',
      metadata_status: '/api/v1/metadata/status',
      metadata_apply: '/api/v1/metadata/apply', metadata_match: '/api/v1/metadata/match',
      metadata_search: '/api/v1/metadata/search',
      import: '/api/v1/import', import_steam: '/api/v1/import/steam',
      import_heroic: '/api/v1/import/heroic', import_lutris: '/api/v1/import/lutris',
      import_arcade: '/api/v1/import/arcade', import_scummvm: '/api/v1/import/scummvm',
      import_rpcs3: '/api/v1/import/rpcs3', import_vita3k: '/api/v1/import/vita3k',
      emulators: '/api/v1/emulators', emulators_install: '/api/v1/emulators/install',
      profiles: '/api/v1/profiles',
      themes: '/api/v1/themes',
      'themes_open-folder': '/api/v1/themes/open-folder',
      update: '/api/v1/update', update_install: '/api/v1/update/install',
      backup: '/api/v1/backup', backup_create: '/api/v1/backup/create',
      backup_restore: '/api/v1/backup/restore', backups: '/api/v1/backups',
      plugins: '/api/v1/plugins',
      'filter-presets': '/api/v1/filter-presets',
      'premium_media-packs': '/api/v1/premium/media-packs',
      'premium_media-packs_apply': '/api/v1/premium/media-packs/apply',
      storefront_import: '/api/v1/storefront/import',
      gameyfin_test: '/api/v1/gameyfin/test',
      ra_inject: '/api/v1/ra/inject',
      bigbox_mode: '/api/v1/bigbox/mode',
      extra_launch: '/api/v1/extra/launch',
    };
    /**
     * Compute the most recent timestamp for a game (last played or added).
     * @param {Record<string, any>} game
     * @returns {number}
     */
    function recentActivityValue(game) {
      const played = Date.parse(game.last_played || '') || 0;
      const added = Date.parse(game.added_at || '') || 0;
      return Math.max(played, added);
    }
    /**
     * Sort game list according to specified sort field.
     * @param {Array<Record<string, any>>} list
     * @param {string} sort
     * @param {string} [dir] 'reversed' flips the comparator (list-view toggle)
     * @returns {Array<Record<string, any>>}
     */
    function sortGames(list, sort, dir) {
      const cmp = (a, b) => sort === 'rating' ? Number(b.rating || 0) - Number(a.rating || 0) || String(a.name || '').localeCompare(String(b.name || ''))
        : sort === 'recent' ? String(b.last_played || '').localeCompare(String(a.last_played || '')) || String(a.sort_title || a.name || '').localeCompare(String(b.sort_title || b.name || ''))
        : sort === 'recent_activity' ? recentActivityValue(b) - recentActivityValue(a) || String(a.name || '').localeCompare(String(b.name || ''))
        : sort === 'playtime' ? Number(b.playtime_seconds || 0) - Number(a.playtime_seconds || 0) || String(a.sort_title || a.name || '').localeCompare(String(b.sort_title || b.name || ''))
        : sort === 'added' ? String(b.added_at || '').localeCompare(String(a.added_at || '')) || String(a.sort_title || a.name || '').localeCompare(String(b.sort_title || b.name || ''))
        : sort === 'platform' ? String(a.platform || '').localeCompare(String(b.platform || '')) || String(a.sort_title || a.name || '').localeCompare(String(b.sort_title || b.name || ''))
        : sort === 'genre' ? String(a.genre || '').localeCompare(String(b.genre || '')) || String(a.sort_title || a.name || '').localeCompare(String(b.sort_title || b.name || ''))
        : String(a.sort_title || a.name || '').localeCompare(String(b.sort_title || b.name || ''));
      return list.sort((a, b) => dir === 'reversed' ? -cmp(a, b) : cmp(a, b));
    }
    const RATIO_BUCKETS = [['portrait','Portrait'],['square','Square'],['landscape','Landscape']];
    const RATIO_REP = {portrait:.72, square:1, landscape:16/9};
    /**
     * Classify aspect ratio into portrait, square, or landscape bucket.
     * @param {number | null | undefined} ratio
     * @returns {'portrait' | 'square' | 'landscape'}
     */
    const coverBucketOf = ratio => ratio == null ? 'portrait' : ratio < .85 ? 'portrait' : ratio <= 1.15 ? 'square' : 'landscape';

    /**
     * Render a metadata fact row HTML snippet.
     * @param {string} label
     * @param {unknown} value
     * @returns {string}
     */
    const fact = (label,value) => `<div class="fact"><small>${escapeHtml(label)}</small><span>${escapeHtml(value ?? '-')}</span></div>`;

    // Shared trigram helpers for worker.search.js parity (identical logic in worker)
    const SEARCH_TRIGRAM_MAX_TERM = 32;
    function trigramsOf(value) {
      const s = String(value || '').toLowerCase();
      const out = new Set();
      for (let i = 0; i <= s.length - 3; i++) out.add(s.slice(i, i + 3));
      for (let i = 0; i <= s.length - 2; i++) out.add(s.slice(i, i + 2));
      return out;
    }
    function expandTrigrams(query) {
      const terms = new Set();
      const words = String(query || '').toLowerCase().match(/[a-z0-9]+/g) || [];
      words.forEach(word => {
        for (const tri of trigramsOf(word)) terms.add(tri);
        const limited = word.slice(0, SEARCH_TRIGRAM_MAX_TERM);
        for (let end = 2; end <= Math.min(limited.length, 9); end++) terms.add(limited.slice(0, end));
        for (let len = 2; len <= Math.min(limited.length, 9); len++) terms.add(limited.slice(limited.length - len));
      });
      if (words.length === 1 && words[0].length >= 2 && words[0].length <= 8) terms.add(words[0]);
      return [...terms];
    }
    function trigramScore(query, haystack) {
      const q = trigramsOf(String(query || '').toLowerCase());
      const h = trigramsOf(String(haystack || '').toLowerCase());
      if (!q.size) return 0;
      let common = 0;
      for (const t of q) if (h.has(t)) common++;
      return common / q.size;
    }

export { $, escapeHtml, duration, formatBytes, safeStorage, prefersReducedMotion, applyGridA11y, gridCellAttrs, formatDate, defaultControllerMap, defaultBadges, artworkKinds, RATIO_BUCKETS, RATIO_REP, coverBucketOf, fact, badge, API_V1, gameInstalled, recentActivityValue, sortGames, parseQueryTokens, advancedQueryMatches, trigramsOf, expandTrigrams, trigramScore };
