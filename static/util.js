



    const defaultControllerMap = {play:0,back:1,favorite:2,random:3,page_left:4,page_right:5,pause:8,menu:9};
    const $ = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const duration = seconds => { const minutes = Math.floor((seconds || 0) / 60), hours = Math.floor(minutes / 60); return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`; };
    const defaultBadges = ['favorite','installed','saves','documents','progress','storefront','achievements','rating'];
    const gameInstalled = game => game.store_installed !== false && (game.path_exists || game.store_installed);
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
    const artworkKinds = [['clear_logo','Clear logo','has_clear_logo'],['fanart','Fanart','has_fanart'],['banner','Banner','has_banner'],['icon','Icon','has_icon'],['box_back','Box back','has_box_back'],['box_spine','Box spine','has_box_spine'],['box_3d','3D box','has_box_3d'],['title_screen','Title screen','has_title_screen'],['cart_front','Cart front','has_cart_front'],['cart_back','Cart back','has_cart_back'],['disc','Disc','has_disc'],['advertisement','Advertisement / flyer','has_advertisement'],['manual','Manual','has_manual']];
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
    const RATIO_BUCKETS = [['portrait','Portrait'],['square','Square'],['landscape','Landscape']];
    const RATIO_REP = {portrait:.72, square:1, landscape:16/9};
    const coverBucketOf = ratio => ratio == null ? 'portrait' : ratio < .85 ? 'portrait' : ratio <= 1.15 ? 'square' : 'landscape';
    const fact = (label,value) => `<div class="fact"><small>${label}</small><span>${escapeHtml(value || '-')}</span></div>`;

export { $, escapeHtml, duration, formatBytes, defaultControllerMap, defaultBadges, artworkKinds, RATIO_BUCKETS, RATIO_REP, coverBucketOf, fact, badge, API_V1, gameInstalled, recentActivityValue, sortGames, parseQueryTokens, advancedQueryMatches };
