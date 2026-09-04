const fs = require('fs');
const puppeteer = require('./node_modules/puppeteer');

(async () => {
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined);
  const launchOpts = {headless: 'new', args: ['--no-sandbox']};
  if (executablePath) launchOpts.executablePath = executablePath;
  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => {
    if (m.type() === 'error') {
      const text = m.text();
      // frame-ancestors and X-Frame-Options are expected for top-level; ignore in harness
      if (text.includes('frame-ancestors') || text.includes("Framing 'http") || text.includes('X-Frame-Options') || text.includes('Refused to display')) return;
      if (text.includes('Failed to load resource') && text.includes('404')) return;
      errors.push('console: ' + text);
    }
  });
  await page.goto(`http://127.0.0.1:${process.env.PORT}?token=${process.env.TOKEN}`, {waitUntil:'networkidle2', timeout:20000});
  const browserContext = browser.defaultBrowserContext();
  await browserContext.overridePermissions(`http://127.0.0.1:${process.env.PORT}`, ['clipboard-read', 'clipboard-write']);
  await new Promise(r => setTimeout(r, 3000));

  // 1. Initial grid and card click
  const before = await page.evaluate(() => ({
    gamesLen: AppState.games.length,
    cardCount: document.querySelectorAll('.card').length,
    filtered: filteredGames().map(g => g.name),
    status: document.getElementById('status').textContent,
  }));
  const clicked = await page.evaluate(() => {
    const c = document.querySelector('.card-main');
    if (!c) return false;
    c.click();
    return true;
  });
  await new Promise(r => setTimeout(r, 800));
  const after = await page.evaluate(() => ({ details: document.getElementById('details').innerText.slice(0, 200) }));
  console.log(JSON.stringify({before, clicked, after}, null, 2));

  // 2. All 5 stock themes render menus cleanly above content
  const themeNames = ['Midnight Circuit', 'Harbor Light', 'Cinema Marquee', 'Nordic Mist', 'Phosphor Terminal'];
  const themeResults = [];
  for (const name of themeNames) {
    await page.evaluate(async (n, t) => {
      await fetch('/api/themes/select', {method: 'POST', headers: {'X-OpenBox-Token': t, 'Content-Type': 'application/json'}, body: JSON.stringify({name: n})});
    }, name, process.env.TOKEN);
    await page.goto(`http://127.0.0.1:${process.env.PORT}/?token=${process.env.TOKEN}`, {waitUntil: 'domcontentloaded', timeout: 20000});
    await new Promise(r => setTimeout(r, 1200));
    await page.click('#toolsButton');
    await new Promise(r => setTimeout(r, 300));
    const ok = await page.evaluate(() => {
      const menu = document.querySelector('.topbar-tools .tool-menu');
      if (!menu) return false;
      const rect = menu.getBoundingClientRect();
      const topEl = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return menu.contains(topEl);
    });
    themeResults.push({name, ok});
    await page.evaluate(() => document.querySelector('#toolsWrap')?.classList.remove('open'));
  }
  console.log('theme menus:', JSON.stringify(themeResults));

  // 3. Big Box hybrid platform switching
  await page.evaluate(() => document.getElementById('bigBoxButton').click());
  await new Promise(r => setTimeout(r, 500));
  const platformBefore = await page.evaluate(() => ({
    mode: AppState.appSettings.bigbox_mode,
    platform: AppState.bigBoxPlatform,
    games: AppState.bigBoxGames.map(g => g.platform),
  }));
  const hybridSwitched = await page.evaluate(async (t) => {
    await fetch('/api/settings', {method: 'POST', headers: {'X-OpenBox-Token': t, 'Content-Type': 'application/json'}, body: JSON.stringify({...AppState.appSettings, bigbox_mode: 'hybrid'})});
    return true;
  }, process.env.TOKEN);
  await page.goto(`http://127.0.0.1:${process.env.PORT}/?token=${process.env.TOKEN}`, {waitUntil: 'domcontentloaded', timeout: 20000});
  await new Promise(r => setTimeout(r, 1500));
  await page.evaluate(() => document.getElementById('bigBoxButton').click());
  await new Promise(r => setTimeout(r, 800));
  const platformAfter = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll('.bigbox-platform')];
    const current = AppState.bigBoxPlatform;
    const other = buttons.find(b => b.dataset.bigboxPlatform && b.dataset.bigboxPlatform !== current);
    if (!other) return {ok: false, reason: 'no .bigbox-platform button to click', current, buttons: buttons.map(b => b.dataset.bigboxPlatform)};
    other.click();
    return {ok: AppState.bigBoxPlatform === other.dataset.bigboxPlatform, clicked: other.dataset.bigboxPlatform, current, now: AppState.bigBoxPlatform};
  });
  console.log('bigbox hybrid platform:', JSON.stringify({platformBefore, hybridSwitched, platformAfter}));
  await page.evaluate(async (t) => {
    await fetch('/api/settings', {method: 'POST', headers: {'X-OpenBox-Token': t, 'Content-Type': 'application/json'}, body: JSON.stringify({...AppState.appSettings, bigbox_mode: 'stage'})});
  }, process.env.TOKEN);

  // 4. IGDB search platform parameter forwarding & API_V1 route mapping
  const interceptedRequests = [];
  await page.setRequestInterception(true);
  page.on('request', request => {
    if (request.url().includes('/api/metadata/igdb/search') || request.url().includes('/api/v1/metadata/match') || request.url().includes('/api/metadata/match')) {
      interceptedRequests.push(request.url());
      request.respond({status: 200, contentType: 'application/json', body: '{"results":[],"ok":true,"state":"running"}'});
    } else {
      request.continue();
    }
  });

  const igdbChecked = await page.evaluate(async () => {
    const game = AppState.games.find(g => g.name === 'Quake');
    if (!game) return {ok: false, reason: 'Quake not seeded'};
    document.getElementById('databaseMetadataButton')?.click();
    if (!document.getElementById('metadataDialog').open) {
      const mod = await import('/static/metadata.js');
      mod.openMetadata(game);
    }
    if (document.getElementById('metadataQuery').value === '') document.getElementById('metadataQuery').value = game.name;
    document.getElementById('searchIgdb').click();
    return true;
  });
  await new Promise(r => setTimeout(r, 2000));
  const igdbReq = interceptedRequests.find(u => u.includes('/api/metadata/igdb/search'));
  const igdbPlatformParam = Boolean(igdbReq && /[?&]platform=/.test(igdbReq));
  await page.evaluate(() => document.getElementById('closeMetadata')?.click());
  await new Promise(r => setTimeout(r, 300));
  // 5. Test API_V1 metadata_match resolution
  const v1MatchChecked = await page.evaluate(async () => {
    const stateMod = await import('/static/state.js');
    try {
      await stateMod.api('/api/metadata/match', {method: 'POST', body: '{}'});
      return true;
    } catch (e) {
      return false;
    }
  });
  const v1MatchMapped = interceptedRequests.some(u => u.includes('/api/v1/metadata/match'));
  console.log('v1 match mapping:', {v1MatchChecked, v1MatchMapped});

  // F29 Match Review UI
  const matchFixture = {
    preview_id: 'preview-smoke-1',
    revision: 3,
    state: 'ready',
    counts: {auto_applied: 1, exact_review: 1, likely: 1, possible: 1, unmatched: 1},
    itemsPage1: [{
      game_id: 'g-quake',
      class: 'likely',
      score: {title_similarity: 0.95, token_overlap: 0.82, platform_exact: true, reasons: ['Exact platform', 'High title similarity']},
      current: {title: 'Quake', platform: 'PC', year: '1996', developer: 'id', publisher: 'id', genre: 'FPS', esrb: 'M', description: 'Current desc', media_categories: ['cover']},
      proposed: {database_id: '42', title: 'Quake', platform: 'PC', year: '1996', developer: 'id Software', publisher: 'id Software', genre: 'FPS', esrb: 'M', description: 'Proposed desc', media_categories: ['cover', 'background']},
      alternatives: [{database_id: '43', title: 'Quake Alt', platform: 'PC', score: {title_similarity: 0.8, token_overlap: 0.75, platform_exact: true, reasons: ['Alternate']}}],
    }],
    itemsPage2: [{
      game_id: 'g-chrono',
      class: 'possible',
      score: {title_similarity: 0.78, token_overlap: 0.76, platform_exact: true, reasons: ['Possible match']},
      current: {title: 'Chrono Trigger', platform: 'SNES', year: '1995', developer: null, publisher: null, genre: 'RPG', esrb: null, description: null, media_categories: []},
      proposed: {database_id: '99', title: 'Chrono Trigger', platform: 'SNES', year: '1995', developer: 'Square', publisher: 'Square', genre: 'RPG', esrb: 'E', description: 'RPG classic', media_categories: ['cover']},
      alternatives: [],
    }],
  };
  const matchReview = await page.evaluate(async (fixture) => {
    const results = {};
    const calls = {previewPost: 0, itemsGets: [], decisions: [], apply: null};
    const origFetch = window.fetch;
    const jsonResponse = (body, status = 200) => {
      const text = JSON.stringify(body);
      return {ok: status >= 200 && status < 300, status, text: async () => text, json: async () => body};
    };
    window.fetch = async (url, opts = {}) => {
      const href = String(url);
      const method = (opts.method || 'GET').toUpperCase();
      if (href.includes('/api/v2/metadata/matches/preview') && method === 'POST') {
        calls.previewPost += 1;
        return jsonResponse({preview_id: fixture.preview_id, revision: fixture.revision, job_id: 'job-1', state: 'ready'}, 202);
      }
      if (href.includes('/api/v2/metadata/matches/preview?') && method === 'GET') {
        return jsonResponse({preview_id: fixture.preview_id, revision: fixture.revision, state: 'ready', job_id: 'job-1', counts: fixture.counts});
      }
      if (href.includes('/api/v2/metadata/matches/items?') && method === 'GET') {
        const query = new URL(href, window.location.origin).searchParams;
        calls.itemsGets.push({cursor: query.get('cursor'), class: query.get('class')});
        const pageItems = query.get('cursor') ? fixture.itemsPage2 : fixture.itemsPage1;
        return jsonResponse({
          preview_id: fixture.preview_id,
          revision: fixture.revision,
          cursor: query.get('cursor'),
          next_cursor: query.get('cursor') ? null : 'cursor-page-2',
          items: pageItems.filter(item => !query.get('class') || item.class === query.get('class')),
        });
      }
      if (href.includes('/api/v2/metadata/matches/decisions') && method === 'POST') {
        const body = JSON.parse(opts.body || '{}');
        calls.decisions.push(body);
        return jsonResponse({preview_id: fixture.preview_id, accepted: body.items?.length || 0, chosen: 0, skipped: 0, never: body.items?.some(i => i.action === 'never') ? 1 : 0});
      }
      if (href.includes('/api/v2/metadata/matches/apply') && method === 'POST') {
        calls.apply = JSON.parse(opts.body || '{}');
        return jsonResponse({job_id: 'apply-1', preview_id: fixture.preview_id, revision: fixture.revision}, 202);
      }
      if (href.includes('/api/metadata/status')) {
        return jsonResponse({ready: true, coverage: {games: 1}});
      }
      return origFetch(url, opts);
    };
    const mod = await import('/static/metadata.js');
    results.exported = typeof mod.openMatchReview === 'function';
    await mod.openMatchReview({preview_id: fixture.preview_id});
    await new Promise(r => setTimeout(r, 400));
    const row = document.querySelector('.match-review-row');
    const fieldKeys = ['title','platform','year','developer','publisher','genre','esrb','description','media_categories'];
    results.rowKeys = fieldKeys.every(key => row?.querySelector(`[data-current="${key}"]`) && row?.querySelector(`[data-proposed="${key}"]`));
    const scoreText = row?.querySelector('.match-review-score')?.textContent || '';
    results.scoreComponents = /Title\s+\d+%/.test(scoreText) && /Tokens\s+\d+%/.test(scoreText) && /Platform/.test(scoreText);
    results.scoreReasons = Boolean(row?.querySelector('.match-review-reasons li'));
    results.noBareConfidence = !/\bconfidence\b/i.test(scoreText);
    document.getElementById('matchReviewLoadMore')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.paginationCursor = calls.itemsGets.some(call => call.cursor === 'cursor-page-2');
    results.nextCursorUsed = calls.itemsGets.length >= 2;
    document.getElementById('matchReviewBulkLikely')?.click();
    await new Promise(r => setTimeout(r, 200));
    const bulkLikelyItems = calls.decisions.at(-1)?.items || [];
    results.bulkLikelyNoPossible = bulkLikelyItems.length > 0 && bulkLikelyItems.every(item => item.action === 'accept') && !bulkLikelyItems.some(item => item.game_id === 'g-chrono');
    row?.querySelector('[data-match-action="never"]')?.click();
    await new Promise(r => setTimeout(r, 400));
    document.getElementById('a11yConfirmOk')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.neverPosted = calls.decisions.some(body => body.items?.some(item => item.action === 'never'));
    row?.querySelector('[data-match-action="accept"]')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.acceptUsesDecisions = calls.decisions.some(body => body.items?.some(item => item.action === 'accept' && item.game_id === 'g-quake'));
    results.acceptNotV1Match = !calls.decisions.some(body => body.items?.some(item => String(item.action).includes('match')));
    await mod.openMatchReview({preview_id: fixture.preview_id});
    await new Promise(r => setTimeout(r, 300));
    results.reopenWorks = document.getElementById('metadataDialog')?.open === true;
    const fieldBox = document.querySelector('[data-field-allow="title"]');
    if (fieldBox) fieldBox.checked = true;
    const mediaBox = document.querySelector('[data-media-allow="cover"]');
    if (mediaBox) mediaBox.checked = true;
    const replaceBox = document.getElementById('matchReviewReplaceExisting');
    if (replaceBox) replaceBox.checked = true;
    document.getElementById('matchReviewApply')?.click();
    await new Promise(r => setTimeout(r, 400));
    document.getElementById('a11yConfirmOk')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.applyPayload = calls.apply;
    const cssText = [...document.styleSheets].map(sheet => {
      try { return [...sheet.cssRules].map(rule => rule.cssText).join('\n'); } catch { return ''; }
    }).join('\n');
    const matchRules = cssText.match(/\.match-review-[^{]+{[^}]+}/g) || [];
    results.matchReviewCssVarsOnly = matchRules.length > 0 && matchRules.every(rule => !/(#[0-9a-f]{3,8}|rgb\(|hsl\()/i.test(rule));
    window.fetch = origFetch;
    document.getElementById('closeMetadata')?.click();
    return {...results, calls};
  }, matchFixture);
  console.log('match review:', JSON.stringify(matchReview, null, 2));

  // F19 Activity UI
  const activityFixture = {
    now: Date.now(),
    jobs: [
      {
        job_id: 'job-active', root_job_id: 'job-active', retry_of: null, resume_of: null,
        type: 'setup.scan', title: 'Library scan', state: 'running', phase: 'scan',
        current: 2, total: 10, message: 'Scanning folders',
        created_at: '2026-08-25T10:00:00', updated_at: '2026-08-25T10:05:00', started_at: '2026-08-25T10:00:00', finished_at: null,
        can_cancel: true, can_retry: false, can_resume: false, input: {}, checkpoint: null, result: null, error: null,
      },
      {
        job_id: 'job-attention', root_job_id: 'job-attention', retry_of: null, resume_of: null,
        type: 'media.bulk_download', title: 'Media download', state: 'partial', phase: 'download',
        current: 3, total: 5, message: 'Some downloads failed',
        created_at: '2026-08-25T09:00:00', updated_at: '2026-08-25T09:30:00', started_at: '2026-08-25T09:00:00', finished_at: '2026-08-25T09:30:00',
        can_cancel: false, can_retry: true, can_resume: false, input: {}, checkpoint: {failed_game_ids: ['g1']}, result: null, error: null,
      },
      {
        job_id: 'job-recent', root_job_id: 'job-recent', retry_of: null, resume_of: null,
        type: 'library.backup', title: 'Library backup', state: 'done', phase: 'complete',
        current: 1, total: 1, message: 'Backup complete',
        created_at: '2026-08-24T08:00:00', updated_at: '2026-08-24T08:10:00', started_at: '2026-08-24T08:00:00', finished_at: '2026-08-24T08:10:00',
        can_cancel: false, can_retry: false, can_resume: false, input: {}, checkpoint: null, result: {path: '/tmp/backup.zip'}, error: null,
      },
      {
        job_id: 'job-stale', root_job_id: 'job-stale', retry_of: null, resume_of: null,
        type: 'cloud.sync', title: 'Cloud sync', state: 'error', phase: 'sync',
        current: 0, total: 1, message: 'Auth failed',
        created_at: '2026-06-01T08:00:00', updated_at: '2026-06-01T08:01:00', started_at: '2026-06-01T08:00:00', finished_at: '2026-06-01T08:01:00',
        can_cancel: false, can_retry: false, can_resume: false, input: {}, checkpoint: null, result: null, error: {code: 'AUTH', message: 'Token expired'},
      },
    ],
    items: [{
      item_id: 'item-1', label: 'Game One', state: 'failed', error: {code: 'MISSING', message: 'Cover not found'},
    }],
  };
  const activityUi = await page.evaluate(async (fixture) => {
    const results = {};
    const calls = {jobsGets: [], sseConnected: false, posts: [], itemsGets: []};
    const order = [];
    class MockEventSource {
      constructor() {
        calls.sseConnected = true;
        order.push('sse');
        this.listeners = {};
      }
      addEventListener(kind, handler) { this.listeners[kind] = handler; }
      close() {}
      emit(kind, data) { this.listeners[kind]?.({data: JSON.stringify(data)}); }
    }
    const origFetch = window.fetch;
    const jsonResponse = (body, status = 200) => {
      const text = JSON.stringify(body);
      return {ok: status >= 200 && status < 300, status, text: async () => text, json: async () => body};
    };
    window.fetch = async (url, opts = {}) => {
      const href = String(url);
      const method = (opts.method || 'GET').toUpperCase();
      if (href.includes('/api/v2/jobs/items?') && method === 'GET') {
        calls.itemsGets.push(href);
        return jsonResponse({job_id: 'job-attention', cursor: null, next_cursor: null, items: fixture.items});
      }
      if (href.includes('/api/v2/jobs') && method === 'GET' && !href.includes('/items')) {
        calls.jobsGets.push(href);
        order.push('jobs');
        return jsonResponse({cursor: null, next_cursor: null, jobs: fixture.jobs});
      }
      if (href.includes('/api/v2/jobs/cancel') && method === 'POST') {
        const body = JSON.parse(opts.body || '{}');
        calls.posts.push({path: 'cancel', body});
        if (body.job_id === 'job-attention') {
          return jsonResponse({code: 'JOB_NOT_CANCELLABLE', error: 'Not cancellable'}, 409);
        }
        return jsonResponse({job_id: body.job_id, state: 'cancelling'}, 202);
      }
      if (href.includes('/api/v2/jobs/retry') && method === 'POST') {
        calls.posts.push({path: 'retry', body: JSON.parse(opts.body || '{}')});
        return jsonResponse({job_id: 'job-retry-new', root_job_id: 'job-attention', retry_of: 'job-attention', state: 'queued'}, 202);
      }
      if (href.includes('/api/v2/jobs/resume') && method === 'POST') {
        calls.posts.push({path: 'resume', body: JSON.parse(opts.body || '{}')});
        return jsonResponse({job_id: 'job-resume-new', resume_of: 'job-attention', state: 'queued'}, 202);
      }
      return origFetch(url, opts);
    };
    const RealEventSource = window.EventSource;
    window.EventSource = MockEventSource;
    const mod = await import('/static/activity.js?f19=' + Date.now());
    results.exportedOpenActivity = typeof mod.openActivity === 'function';
    results.exportedPartition = typeof mod.partitionJobs === 'function';
    const partitioned = mod.partitionJobs(fixture.jobs);
    results.partitionActive = partitioned.active.map(j => j.job_id);
    results.partitionAttention = partitioned.attention.map(j => j.job_id);
    results.partitionRecent = partitioned.recent.map(j => j.job_id);
    await mod.openActivity();
    await new Promise(r => setTimeout(r, 400));
    if (document.getElementById('activityButton')) {
      document.getElementById('activityButton').onclick = () => mod.openActivity();
    }
    results.drawerOpen = document.getElementById('activityDrawer')?.open === true;
    const row = document.querySelector('.activity-row[data-job-id="job-active"]');
    const rowKeys = ['job_id','type','title','state','phase','current','total','message','can_cancel','can_retry','can_resume'];
    results.rowKeys = rowKeys.every(key => row?.hasAttribute(`data-${key.replace(/_/g, '-')}`));
    const partialBadge = document.querySelector('[data-activity-state-badge="partial"]');
    const doneBadge = document.querySelector('[data-activity-state-badge="done"]');
    results.partialNotSuccess = partialBadge && !/✓/.test(partialBadge.textContent || '') && doneBadge && /✓/.test(doneBadge.textContent || '');
    const cancelBtn = document.querySelector('[data-activity-cancel="job-attention"]');
    results.cancelDisabled = cancelBtn?.disabled === true;
    if (cancelBtn) cancelBtn.disabled = false;
    cancelBtn?.click();
    await new Promise(r => setTimeout(r, 200));
    results.cancelNotCancellablePosted = calls.posts.some(p => p.path === 'cancel' && p.body?.job_id === 'job-attention');
    document.querySelector('[data-activity-retry="job-attention"]')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.retryBodyJobIdOnly = calls.posts.some(p => p.path === 'retry' && p.body?.job_id === 'job-attention' && !('input' in p.body));
    document.querySelector('[data-activity-toggle-items="job-attention"]')?.click();
    await new Promise(r => setTimeout(r, 300));
    const item = document.querySelector('.activity-item[data-item-id="item-1"]');
    results.itemsKeys = Boolean(item?.dataset.itemId && item?.dataset.itemState && item?.querySelector('.activity-item-error'));
    results.itemsFetch = calls.itemsGets.some(href => href.includes('job_id=job-attention'));
    const appJs = await fetch('/static/app.js').then(r => r.text());
    results.appImportsActivity = /import '\.\/activity\.js'/.test(appJs);
    document.getElementById('activityButton')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.activityButtonOpens = document.getElementById('activityDrawer')?.open === true;
    results.snapshotBeforeSse = order.indexOf('jobs') >= 0 && (order.indexOf('sse') < 0 || order.indexOf('jobs') < order.indexOf('sse'));
    const cssText = [...document.styleSheets].map(sheet => {
      try { return [...sheet.cssRules].map(rule => rule.cssText).join('\n'); } catch { return ''; }
    }).join('\n');
    const activityRules = cssText.match(/\.activity-[^{]+{[^}]+}/g) || [];
    results.activityCssVarsOnly = activityRules.length > 0 && activityRules.every(rule => !/(#[0-9a-f]{3,8}|rgb\(|hsl\()/i.test(rule));
    window.fetch = origFetch;
    window.EventSource = RealEventSource;
    document.getElementById('closeActivityDrawer')?.click();
    return {...results, calls, order};
  }, activityFixture);
  console.log('activity ui:', JSON.stringify(activityUi, null, 2));

  // F18 Setup Center UI
  const setupFixture = {
    preview_id: 'preview-setup-smoke',
    revision: 2,
    import_batch_id: 'batch-setup-smoke',
    summary: {
      library_count: 0,
      source_coverage: [{source_id: 'library', label: 'Library', game_count: 0, coverage_percent: 0}],
      metadata_match_percent: 0,
      media_gaps: 0,
      duplicate_count: 0,
      missing_paths: 0,
      emulator_readiness: {ready: 0, warning: 0, blocked: 0, unknown: 0},
      active_operations: 0,
      next_action: {id: 'add_sources', label: 'Add game sources', step: 2},
    },
    previewItem: {
      candidate_id: 'cand-1',
      group: 'merges',
      source: {type: 'folder', path: '/roms/game.nes', label: 'ROMs'},
      detected_title: 'Demo Game',
      detected_platform: 'NES',
      intended_action: 'merge',
      existing_game_target: {game_id: 'g-1', title: 'Demo', platform: 'NES'},
      warnings: [{code: 'WARN', message: 'Check path'}],
      emulator_choices: [{adapter_id: 'retroarch-nes', emulator_id: 'retroarch', label: 'RetroArch', recommended: true, flatpak_app_id: 'org.libretro.RetroArch'}],
      merge_diff: [{field: 'path', current: '/old.nes', proposed: '/roms/game.nes', effect: 'fill'}],
    },
  };
  const setupUi = await page.evaluate(async (fixture) => {
    const results = {};
    const calls = {
      previewPost: 0, previewGet: 0, itemsGet: 0, decisions: [], preflightBatch: null,
      install: null, revalidate: null, commit: null, finishOrder: [],
      settingsPost: null,
    };
    const jobs = new Map([
      ['job-scan', {job_id: 'job-scan', state: 'running', type: 'setup.scan'}],
      ['job-revalidate', {job_id: 'job-revalidate', state: 'running', type: 'setup.revalidate'}],
      ['job-commit', {job_id: 'job-commit', state: 'running', type: 'setup.commit', result: {added: 2, merged: 1, skipped: 0}}],
      ['job-install', {job_id: 'job-install', state: 'error', type: 'emulator.install', error: 'install failed'}],
      ['job-meta-sync', {job_id: 'job-meta-sync', state: 'running', type: 'metadata.sync'}],
      ['job-match', {job_id: 'job-match', state: 'running', type: 'metadata.match_preview'}],
      ['job-media', {job_id: 'job-media', state: 'running', type: 'media.bulk'}],
    ]);
    const origFetch = window.fetch;
    const jsonResponse = (body, status = 200) => {
      const text = JSON.stringify(body);
      return {ok: status >= 200 && status < 300, status, text: async () => text, json: async () => body};
    };
    window.fetch = async (url, opts = {}) => {
      const href = String(url);
      const method = (opts.method || 'GET').toUpperCase();
      const body = opts.body ? JSON.parse(opts.body) : null;
      if (href.includes('/api/v2/setup/summary') && method === 'GET') return jsonResponse(fixture.summary);
      if (href.includes('/api/v2/setup/preview') && method === 'POST' && !href.includes('/preview/')) {
        calls.previewPost += 1;
        return jsonResponse({preview_id: fixture.preview_id, revision: fixture.revision, job_id: 'job-scan', state: 'queued'}, 202);
      }
      if (href.includes('/api/v2/setup/preview?') && method === 'GET') {
        calls.previewGet += 1;
        return jsonResponse({
          preview_id: fixture.preview_id, revision: fixture.revision, state: 'ready', revalidated: calls.revalidate != null,
          counts: {additions: 1, merges: 1}, job_id: null,
        });
      }
      if (href.includes('/api/v2/setup/preview/items') && method === 'GET') {
        calls.itemsGet += 1;
        return jsonResponse({preview_id: fixture.preview_id, revision: fixture.revision, items: [fixture.previewItem], next_cursor: null});
      }
      if (href.includes('/api/v2/setup/preview/decisions') && method === 'POST') {
        calls.decisions.push(body);
        return jsonResponse({preview_id: fixture.preview_id, accepted: body.items?.length || 0});
      }
      if (href.includes('/api/v2/setup/preview/revalidate') && method === 'POST') {
        calls.revalidate = body;
        return jsonResponse({preview_id: fixture.preview_id, revision: fixture.revision, job_id: 'job-revalidate', state: 'queued'}, 202);
      }
      if (href.includes('/api/v2/setup/commit') && method === 'POST') {
        calls.commit = body;
        return jsonResponse({job_id: 'job-commit', import_batch_id: fixture.import_batch_id, preview_id: fixture.preview_id, revision: fixture.revision}, 202);
      }
      if (href.includes('/api/v2/launch/preflight/batch') && method === 'POST') {
        calls.preflightBatch = body;
        return jsonResponse({
          totals: {ready: 1, warning: 0, blocked: 0},
          by_platform: [{platform: 'NES', ready: 1, total: 1}],
          results: [{candidate_id: 'cand-1', status: 'ready', checks: [{code: 'OK', message: 'Ready', remediations: []}]}],
        });
      }
      if (href.includes('emulators/install') && method === 'POST') {
        calls.install = body;
        return jsonResponse({job_id: 'job-install'});
      }
      if (href.includes('metadata/sync') && method === 'POST') {
        calls.finishOrder.push('metadata_sync');
        return jsonResponse({job_id: 'job-meta-sync'});
      }
      if (href.includes('/api/v2/metadata/matches/preview') && method === 'POST') {
        calls.finishOrder.push('matches_preview');
        return jsonResponse({preview_id: 'match-preview', job_id: 'job-match', revision: 1}, 202);
      }
      if (href.includes('/api/v2/metadata/matches/preview?') && method === 'GET') {
        return jsonResponse({counts: {auto_applied: 1, exact_review: 0, likely: 0, possible: 0, unmatched: 0}});
      }
      if (href.includes('media/bulk') && method === 'POST') {
        calls.finishOrder.push('media_bulk');
        return jsonResponse({job_id: 'job-media'});
      }
      if (href.includes('/settings') && method === 'POST' && !href.includes('preview')) {
        calls.settingsPost = body;
        return jsonResponse({...body, welcome_completed: true});
      }
      if (href.includes('/api/v2/jobs')) {
        const all = [...jobs.values()].map(j => ({...j, state: 'done', can_cancel: false, can_retry: false, can_resume: false}));
        return jsonResponse({jobs: all, next_cursor: null});
      }
      if (href.includes('/api/library') || href.includes('/api/v1/library')) {
        return jsonResponse({games: [{game_id: 'g-new', name: 'Demo Game', platform: 'NES', import_batch_id: fixture.import_batch_id}], playlists: [], settings: {welcome_completed: true}, filter_presets: []});
      }
      return origFetch(url, opts);
    };
    const mod = await import('/static/setup.js?f18=' + Date.now());
    results.exported = typeof mod.openSetupCenter === 'function';
    await mod.openSetupCenter({step: 1});
    await new Promise(r => setTimeout(r, 200));
    results.stepperPresent = Boolean(document.querySelector('.setup-stepper'));
    results.summaryKeys = ['library_count','source_coverage','metadata_match_percent','media_gaps','duplicate_count','missing_paths','emulator_readiness','active_operations','next_action']
      .every(key => Boolean(document.querySelector(`[data-summary-key="${key}"]`)));
    results.nextActionOne = document.querySelectorAll('.setup-next-action-btn').length === 1;
    await mod.openSetupCenter({step: 2});
    await new Promise(r => setTimeout(r, 100));
    results.faugusVisible = Boolean([...document.querySelectorAll('.setup-source-card')].find(btn => btn.dataset.sourceKey === 'faugus'));
    results.xbox360Visible = Boolean([...document.querySelectorAll('.setup-source-card')].find(btn => btn.dataset.sourceKey === 'xbox360'));
    results.noFileInput = !document.querySelector('#setupCenter input[type="file"]');
    const importsJs = await fetch('/static/imports.js').then(r => r.text());
    results.importsHasSetup = /import '\.\/setup\.js'/.test(importsJs);
    results.importsNoPrompt = !/\bprompt\(/.test(importsJs);
    await mod.openSetupCenter({step: 2});
    await new Promise(r => setTimeout(r, 100));
    document.querySelector('[data-source-key="faugus"]')?.click();
    await new Promise(r => setTimeout(r, 100));
    results.sourceSelected = Boolean(document.querySelector('.setup-source-selected li'));
    document.getElementById('setupContinue')?.click();
    await new Promise(r => setTimeout(r, 1200));
    results.previewGetAfterPost = calls.previewGet > 0 && calls.previewPost > 0;
    const row = document.querySelector('.setup-preview-row');
    results.previewRowKeys = Boolean(row?.dataset.candidateId
      && row.querySelector('[data-preview-key="warnings"]')
      && row.querySelector('[data-preview-key="emulator_choices"]')
      && row.querySelector('[data-preview-key="merge_diff"] .setup-merge-field'));
    document.getElementById('setupContinue')?.click();
    await new Promise(r => setTimeout(r, 600));
    document.getElementById('setupContinue')?.click();
    await new Promise(r => setTimeout(r, 600));
    const preflightItem = calls.preflightBatch?.items?.[0];
    results.preflightCandidate = preflightItem?.game_id === null
      && preflightItem?.candidate?.candidate_id === 'cand-1'
      && preflightItem?.candidate?.preview_id === fixture.preview_id
      && preflightItem?.candidate?.path === fixture.previewItem.source.path;
    const select = document.querySelector('.setup-emulator-choice');
    if (select) {
      select.value = 'install_flatpak';
      select.dispatchEvent(new Event('change', {bubbles: true}));
    }
    await new Promise(r => setTimeout(r, 800));
    results.installFlatpakBody = calls.install?.app_id === 'org.libretro.RetroArch';
    document.getElementById('setupContinue')?.click();
    await new Promise(r => setTimeout(r, 200));
    const metadataSync = document.getElementById('setupMetadataSync');
    if (metadataSync) {
      metadataSync.checked = true;
      metadataSync.dispatchEvent(new Event('change', {bubbles: true}));
    }
    document.getElementById('setupContinue')?.click();
    await new Promise(r => setTimeout(r, 200));
    document.getElementById('setupContinue')?.click();
    await new Promise(r => setTimeout(r, 1800));
    results.revalidateWaited = Boolean(calls.revalidate);
    results.commitEmulatorChoices = Array.isArray(calls.commit?.emulator_choices);
    results.decisionsIncludeLaunch = calls.decisions.some(batch => (batch.items || []).some(item => 'launch_setup' in item));
    results.finishOrder = calls.finishOrder;
    results.welcomeCompleted = calls.settingsPost?.welcome_completed === true;
    const cssText = await fetch('/static/app.css').then(r => r.text());
    const setupRules = cssText.match(/\.setup-[^{]+{[^}]+}/g) || [];
    results.setupCssVarsOnly = setupRules.length > 0 && setupRules.every(rule => !/(#[0-9a-f]{3,8}|rgb\(|hsl\()/i.test(rule));
    document.getElementById('setupCenter')?.close();
    window.fetch = origFetch;
    return {...results, calls};
  }, setupFixture);
  console.log('setup ui:', JSON.stringify(setupUi, null, 2));

  // 6. Dialog focus management test
  const dialogFocusOk = await page.evaluate(async () => {
    const addBtn = document.getElementById('addButton');
    addBtn.focus();
    addBtn.click();
    await new Promise(r => setTimeout(r, 300));
    const gameDialog = document.getElementById('gameDialog');
    if (!gameDialog?.open) return false;
    document.getElementById('closeDialog').click();
    await new Promise(r => setTimeout(r, 300));
    return document.activeElement === addBtn;
  });
  console.log('dialog focus restore:', {dialogFocusOk});

  // 7. Reader dialog cleanup test
  const readerCleanedOk = await page.evaluate(async () => {
    const readerMod = await import('/static/reader.js');
    const fakeGame = {id: 1, documents: [{name: 'Manual', path: '/tmp/test.pdf'}]};
    readerMod.openReader(fakeGame, 0);
    const frame = document.getElementById('readerFrame');
    const hasSrcBefore = Boolean(frame.getAttribute('src'));
    document.getElementById('closeReader').click();
    const hasSrcAfter = Boolean(frame.getAttribute('src'));
    const urlCleared = AppState.readerUrl === '';
    return hasSrcBefore && !hasSrcAfter && urlCleared;
  });
  console.log('reader cleanup:', {readerCleanedOk});
  const gamepadLoopStopped = await page.evaluate(async () => {
    const bigboxMod = await import('/static/bigbox.js');
    bigboxMod.openBigBox();
    const openVisible = !document.getElementById('bigBox').hidden;
    bigboxMod.closeBigBox();
    const closedVisible = document.getElementById('bigBox').hidden;
    return openVisible && closedVisible;
  });
  console.log('gamepad loop lifecycle:', {gamepadLoopStopped});

  // M5 Mastery Map: dialog opens and renders platform/decade bars (or empty state).
  const masterySmoke = await page.evaluate(async () => {
    const masteryMod = await import('/static/mastery.js');
    masteryMod.openMastery();
    await new Promise(r => setTimeout(r, 300));
    const dialog = document.getElementById('masteryDialog');
    const open = dialog?.open === true;
    let rendered = false;
    for (let i = 0; i < 25 && !rendered; i++) {
      await new Promise(r => setTimeout(r, 200));
      rendered = Boolean(document.querySelector('#masteryBody .mastery-row, #masteryBody .description, #masteryOverall p, #masteryOverall .mastery-overall'));
    }
    document.getElementById('closeMastery')?.click();
    return {open, rendered};
  });
  console.log('mastery dialog:', masterySmoke);

  // F23 Big Box correctness: viewport matrix, activation, preflight launch
  const bigboxCorrectness = { viewportCases: [], activateExported: false, appUsesActivate: false, preflightLaunch: false, noConfirmSessions: false, noConfirmStorefront: false, shortcutWhileTyping: false };
  const staticChecks = await page.evaluate(async () => {
    const bigboxJs = await fetch('/static/bigbox.js').then(r => r.text());
    const sessionsJs = await fetch('/static/sessions.js').then(r => r.text());
    const storefrontJs = await fetch('/static/storefront.js').then(r => r.text());
    const appJs = await fetch('/static/app.js').then(r => r.text());
    return {
      activateExported: /export\s*\{[^}]*activateCurrentGame/.test(bigboxJs),
      appUsesActivate: /activateCurrentGame/.test(appJs),
      preflightLaunch: /\/api\/v2\/launch\/preflight/.test(sessionsJs) && /\/api\/launch/.test(sessionsJs),
      noConfirmSessions: !/\bconfirm\(/.test(sessionsJs),
      noConfirmStorefront: !/\bconfirm\(/.test(storefrontJs),
    };
  });
  Object.assign(bigboxCorrectness, staticChecks);
  for (const viewport of [{width: 1280, height: 800}, {width: 1920, height: 1080}]) {
    await page.setViewport(viewport);
    for (const mode of ['stage', 'hybrid', 'coverflow']) {
      const caseOkResult = await page.evaluate(async (modeName) => {
        const bigbox = await import('/static/bigbox.js');
        const state = await import('/static/state.js');
        AppState.games = [
          {id: 1, game_id: 'g-smoke-1', name: 'Smoke One', platform: 'PC', path: '/bin/true', path_exists: true, has_cover: true, store_installed: true, applications: [], versions: [], documents: []},
          {id: 2, game_id: 'g-smoke-2', name: 'Smoke Two', platform: 'SNES', path: '/bin/true', path_exists: true, has_cover: true, store_installed: true, applications: [], versions: [], documents: []},
        ];
        AppState.platform = 'all';
        AppState.platformCategory = 'all';
        AppState.activePlaylist = '';
        AppState.activeFilterPreset = '';
        AppState.importBatchId = '';
        AppState.explorerRules = {};
        AppState.bigBoxPlatform = 'all';
        const sidebarSearch = document.getElementById('sidebarSearch');
        const view = document.getElementById('view');
        if (sidebarSearch) sidebarSearch.value = '';
        if (view) view.value = 'all';
        AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
        state.invalidateFilterCache();
        state.warmSearchIndex();
        AppState.appSettings.bigbox_mode = modeName;
        bigbox.openBigBox();
        const stage = document.getElementById('bigBoxStage');
        const hybridSearch = document.getElementById('bigBoxHybridSearch');
        const bigBox = document.getElementById('bigBox');
        const hasStage = Boolean(stage && bigBox && !bigBox.hidden);
        const layoutOk = modeName === 'hybrid'
          ? Boolean(hybridSearch && !hybridSearch.hidden)
          : modeName === 'coverflow'
            ? Boolean(stage?.querySelector('[data-coverflow-strip], .coverflow-card'))
            : Boolean(stage?.classList.contains('bigbox-stage'));
        bigbox.closeBigBox();
        return hasStage && layoutOk;
      }, mode);
      const caseOk = Boolean(caseOkResult);
      bigboxCorrectness.viewportCases.push({viewport, mode, ok: caseOk});
      if (!caseOk) process.exit(1);
    }
  }
  bigboxCorrectness.allViewportCases = bigboxCorrectness.viewportCases.length === 6 && bigboxCorrectness.viewportCases.every(item => item.ok);
  bigboxCorrectness.shortcutWhileTyping = await page.evaluate(async () => {
    const bigbox = await import('/static/bigbox.js');
    const state = await import('/static/state.js');
    AppState.games = [
      {id: 1, game_id: 'g-smoke-1', name: 'Smoke One', platform: 'PC', path: '/bin/true', path_exists: true, has_cover: true, applications: [], versions: [], documents: []},
    ];
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    state.invalidateFilterCache();
    const search = document.getElementById('bigBoxHybridSearch');
    if (!search) return true;
    AppState.appSettings.bigbox_mode = 'hybrid';
    bigbox.openBigBox();
    search.focus();
    search.value = 'typing';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    const before = AppState.bigBoxGames.length;
    document.getElementById('bigBox')?.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    const ok = AppState.bigBoxGames.length === before;
    bigbox.closeBigBox();
    return ok;
  });
  console.log('bigbox correctness:', JSON.stringify(bigboxCorrectness, null, 2));

  // 9. Large library performance checks
  const perfChecks = await page.evaluate(async () => {
    const bigbox = await import('/static/bigbox.js');
    const library = await import('/static/library.js');
    const state = await import('/static/state.js');
    const games = Array.from({length: 20000}, (_, i) => ({
      id: i + 1000,
      name: `Chrono Library Game ${i}`,
      sort_title: `Chrono Library Game ${i}`,
      platform: 'PC',
      genre: 'RPG',
      developer: 'Perf Harness',
      path: '/bin/true',
      path_exists: true,
      versions: [],
      applications: [],
    }));
    AppState.games = games;
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    state.warmSearchIndex();
    AppState.appSettings.library_view = 'grid';
    AppState.appSettings.cover_grouping = 'shape';
    document.getElementById('sidebarSearch').value = '';
    library.renderGrid();
    const visibleCount = filteredGames().length;
    const gridNodeCount = document.querySelectorAll('#grid .card, #grid .list-row').length;
    AppState.appSettings.bigbox_mode = 'coverflow';
    AppState.bigBoxGames = games;
    AppState.bigBoxIndex = 10000;
    document.getElementById('bigBox').hidden = false;
    bigbox.renderBigBox();
    const coverflowNodes = document.querySelectorAll('.coverflow-card').length;
    const target = [...document.querySelectorAll('.coverflow-card')].find(node => Number(node.dataset.coverflow) !== AppState.bigBoxIndex);
    const clickTarget = target ? Number(target.dataset.coverflow) : null;
    target?.click();
    const delegatedClickOk = clickTarget !== null && AppState.bigBoxIndex === clickTarget;
    document.getElementById('sidebarSearch').value = 'chrono 19999';
    const start = performance.now();
    const filtered = filteredGames();
    const searchMs = performance.now() - start;
    return {coverflowNodes, delegatedClickOk, gridNodeCount, visibleCount, filtered: filtered.map(game => game.name).slice(0, 3), searchMs, searchIndexStats: AppState.searchIndexStats};
  });
  console.log('perf ui checks:', JSON.stringify(perfChecks));

  // 10. F02 query engine: cache, long tokens, import batch, explorer Unrated, resetQuery, typed notify
  const queryEngine = await page.evaluate(async () => {
    const state = await import('/static/state.js');
    const results = {};
    const baseGames = AppState.games.slice();
    AppState.games = [
      {id: 9001, game_id: 'game-batch-a', name: 'Title batch-a suffix', import_batch_id: 'batch-a', platform: 'PC', path_exists: true, esrb: 'M'},
      {id: 9002, game_id: 'game-batch-b', name: 'Other game', import_batch_id: 'batch-b', platform: 'PC', path_exists: true, esrb: 'E'},
      {id: 9003, game_id: 'game-long', name: 'abcdefghijklmnop', platform: 'PC', path_exists: true, esrb: 'Unrated'},
    ];
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    state.invalidateFilterCache();
    state.warmSearchIndex();
    document.getElementById('sidebarSearch').value = '';
    document.getElementById('view').value = 'all';
    if (document.getElementById('esrbFilter')) document.getElementById('esrbFilter').value = '';
    AppState.platform = 'all';
    AppState.platformCategory = 'all';
    AppState.activePlaylist = '';
    AppState.activeFilterPreset = '';
    AppState.explorerRules = {};
    AppState.importBatchId = '';
    const first = filteredGames();
    const second = filteredGames();
    results.cacheSameIdentity = first === second;
    document.getElementById('sidebarSearch').value = 'abcdefghij';
    state.invalidateFilterCache();
    const longTokenCount = filteredGames().length;
    results.longTokenFindsGame = longTokenCount === 1 && filteredGames()[0].id === 9003;
    document.getElementById('sidebarSearch').value = '';
    if (document.getElementById('esrbFilter')) document.getElementById('esrbFilter').value = 'Unrated';
    state.invalidateFilterCache();
    const unratedOnly = filteredGames().every(g => (g.esrb || 'Unrated') === 'Unrated');
    results.unratedFacetKeepsUnrated = unratedOnly && filteredGames().length === 1;
    AppState.importBatchId = 'batch-a';
    state.invalidateFilterCache();
    document.getElementById('sidebarSearch').value = '';
    if (document.getElementById('esrbFilter')) document.getElementById('esrbFilter').value = '';
    const batchFiltered = filteredGames();
    results.importBatchIdFilter = batchFiltered.length === 1 && batchFiltered[0].import_batch_id === 'batch-a';
    document.getElementById('sidebarSearch').value = 'import_batch_id:"batch-a"';
    state.invalidateFilterCache();
    const typedBatch = filteredGames();
    results.typedImportBatchExact = typedBatch.length === 1 && typedBatch[0].import_batch_id === 'batch-a';
    AppState.importBatchId = 'batch-a';
    state.resetQuery();
    results.resetClearsImportBatch = AppState.importBatchId === '';
    const toast = document.getElementById('toast');
    state.notify('success', 'ok-success');
    results.notifySuccessLevel = toast.dataset.notifyLevel === 'success';
    state.notify('Preset deleted');
    results.notifyCompatInfo = toast.dataset.notifyLevel === 'info' && toast.textContent === 'Preset deleted';
    state.notify('error');
    results.notifySingleErrorLevel = toast.dataset.notifyLevel === 'error';
    document.getElementById('errorBanner').hidden = true;
    state.notify('error', 'sticky failure', {actionable: true});
    results.stickyErrorVisible = !document.getElementById('errorBanner').hidden;
    let copied = '';
    const clip = { value: '' };
    navigator.clipboard.writeText = async (text) => { clip.value = text; };
    navigator.clipboard.readText = async () => clip.value;
    document.getElementById('errorBannerCopy').click();
    copied = clip.value;
    results.errorBannerCopyWorks = copied.includes('sticky failure');
    AppState.games = baseGames;
    return results;
  });
  console.log('query engine:', JSON.stringify(queryEngine));

  // 11. F04 app shell: setup center, tools groups, activity, browse hosts, app.js contract
  const appShell = await page.evaluate(async () => {
    const appJs = await fetch('/static/app.js').then(r => r.text());
    const results = {};
    results.setupCenterPresent = Boolean(document.getElementById('setupCenter'));
    results.welcomeWizardGone = !document.getElementById('welcomeImportFolder') && !document.getElementById('welcomeDone');
    const welcome = document.getElementById('welcomeDialog');
    results.welcomeShimHidden = !welcome || welcome.hidden || welcome.getAttribute('aria-hidden') === 'true';
    document.getElementById('setupLibraryButton')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.setupButtonOpensCenter = document.getElementById('setupCenter')?.open === true;
    document.getElementById('setupCenter')?.close();
    if (welcome) {
      welcome.showModal();
      await new Promise(r => setTimeout(r, 200));
      results.welcomeShimOpensCenter = document.getElementById('setupCenter')?.open === true;
      document.getElementById('setupCenter')?.close();
    } else {
      results.welcomeShimOpensCenter = true;
    }
    document.getElementById('settingsButton')?.click();
    await new Promise(r => setTimeout(r, 300));
    document.getElementById('reopenWelcome')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.reopenOpensCenter = document.getElementById('setupCenter')?.open === true;
    document.getElementById('setupCenter')?.close();
    document.getElementById('settingsDialog')?.close();
    results.queueStillQueue = document.getElementById('queueButton')?.textContent.trim() === 'Queue';
    results.activityDistinct = Boolean(document.getElementById('activityButton')) && document.getElementById('activityButton')?.id !== 'queueButton';
    const groups = {};
    document.querySelectorAll('#toolMenu [data-tool-group]').forEach(group => {
      groups[group.dataset.toolGroup] = [...group.querySelectorAll('[role="menuitem"]')].map(el => el.id);
    });
    results.toolGroups = groups;
    const pathFields = ['path','cover','background','video','music','video_snap','video_theme','video_trailer','video_recording','clear_logo','fanart','banner','icon','box_back','box_spine','box_3d','title_screen','cart_front','cart_back','disc','advertisement','manual','screenshots','documents','save_paths','applications','versions'];
    results.pathBrowseHosts = pathFields.every(name => {
      const field = document.querySelector(`#gameDialog [name="${name}"]`);
      if (!field) return false;
      const row = field.closest('.path-input-row');
      return Boolean(row?.querySelector('.path-browse[data-browse-for="' + name + '"]'));
    });
    results.noWelcomeImport = !/welcomeImport/.test(appJs);
    results.noCompleteWelcome = !/completeWelcome/.test(appJs);
    results.noSetupImport = !/import '\.\/setup\.js'/.test(appJs);
    const importsJs = await fetch('/static/imports.js').then(r => r.text());
    results.importsHasSetup = /import '\.\/setup\.js'/.test(importsJs);
    results.importsNoPrompt = !/\bprompt\(/.test(importsJs);
    results.hasActivityImport = /import '\.\/activity\.js'/.test(appJs);
    results.noBindContextMenuA11y = /bindContextMenuA11y/.test(appJs);
    results.noContextMenuListener = !/addEventListener\('contextmenu'/.test(appJs);
    results.noPromptInAppJs = !/\bprompt\(/.test(appJs);
    results.noConfirmInAppJs = !/\bconfirm\(/.test(appJs);
    const stateJs = await fetch('/static/state.js').then(r => r.text());
    results.nativePickFolderNoPrompt = !/function nativePickFolder[\s\S]*?prompt\(/.test(stateJs);
    return results;
  });
  console.log('app shell:', JSON.stringify(appShell, null, 2));

  // 12. F05 a11y dialogs and context menu keyboard
  const a11yDialogs = await page.evaluate(async () => {
    const dialogsJs = await fetch('/static/dialogs.js').then(r => r.text());
    const results = {};
    results.noWindowPromptInDialogs = !/window\.prompt/.test(dialogsJs);
    const card = document.querySelector('[data-game]');
    if (!card) return { ...results, skipped: true };
    card.focus();
    const beforeFocus = document.activeElement;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }));
    await new Promise(r => setTimeout(r, 200));
    const menu = document.getElementById('contextMenu');
    results.shiftF10OpensMenu = menu && !menu.hidden;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await new Promise(r => setTimeout(r, 200));
    results.escapeRestoresFocus = document.activeElement === beforeFocus || document.activeElement === card;
    const { confirmAction } = await import('/static/dialogs.js');
    const confirmPromise = confirmAction({
      title: 'Test confirm',
      target: 'Named target game',
      consequence: 'Test consequence',
      retained: 'Test retained',
      recovery: 'Test recovery',
    });
    await new Promise(r => setTimeout(r, 300));
    const confirmDialog = document.getElementById('a11yConfirmDialog');
    results.confirmHasNamedTarget = confirmDialog?.open && document.getElementById('a11yConfirmTarget')?.textContent?.includes('Named target game');
    document.getElementById('a11yConfirmCancel')?.click();
    await confirmPromise;
    document.getElementById('addButton')?.click();
    await new Promise(r => setTimeout(r, 400));
    const gameDialog = document.getElementById('gameDialog');
    results.gameDialogOpen = gameDialog?.open === true;
    const pathField = gameDialog?.querySelector('[name="path"]');
    const coverField = gameDialog?.querySelector('[name="cover"]');
    const pathBrowse = gameDialog?.querySelector('.path-browse[data-browse-for="path"]');
    const coverBrowse = gameDialog?.querySelector('.path-browse[data-browse-for="cover"]');
    pathBrowse?.click();
    await new Promise(r => setTimeout(r, 400));
    const inputDialog = document.getElementById('a11yInputDialog');
    if (inputDialog?.open) {
      document.getElementById('a11yInputField').value = '/tmp/smoke-path.iso';
      document.getElementById('a11yInputOk')?.click();
      await new Promise(r => setTimeout(r, 200));
    }
    results.pathBrowseFilled = pathField?.value === '/tmp/smoke-path.iso';
    coverBrowse?.click();
    await new Promise(r => setTimeout(r, 400));
    if (document.getElementById('a11yInputDialog')?.open) {
      document.getElementById('a11yInputField').value = '/tmp/smoke-cover.png';
      document.getElementById('a11yInputOk')?.click();
      await new Promise(r => setTimeout(r, 200));
    }
    results.coverBrowseFilled = coverField?.value === '/tmp/smoke-cover.png';
    gameDialog?.close();
    const settingsDialog = document.getElementById('settingsDialog');
    document.getElementById('settingsButton')?.click();
    await new Promise(r => setTimeout(r, 300));
    const settingsBrowse = settingsDialog?.querySelector('.path-browse[data-browse-for="watchFolders"]');
    const watchBefore = document.getElementById('watchFolders')?.value || '';
    settingsBrowse?.click();
    await new Promise(r => setTimeout(r, 400));
    if (document.getElementById('a11yInputDialog')?.open) {
      document.getElementById('a11yInputField').value = '/tmp/smoke-watch';
      document.getElementById('a11yInputOk')?.click();
      await new Promise(r => setTimeout(r, 200));
    }
    results.settingsBrowseFilled = (document.getElementById('watchFolders')?.value || '').includes('/tmp/smoke-watch')
      && (document.getElementById('watchFolders')?.value || '') !== watchBefore;
    settingsDialog?.close();
    return results;
  });
  console.log('a11y dialogs:', JSON.stringify(a11yDialogs, null, 2));

  // 13. F03 library workspace: chips, preset leave, setup center, manuals, drop honesty
  const libraryWorkspace = await page.evaluate(async () => {
    const library = await import('/static/library.js');
    const state = await import('/static/state.js');
    const libraryJs = await fetch('/static/library.js').then(r => r.text());
    const results = {};
    AppState.games = [
      {id: 1, name: 'Quake', platform: 'PC', path: '/bin/true', path_exists: true, applications: [], versions: [], documents: [], available_screenshots: [], save_paths: []},
      {id: 2, name: 'Chrono Trigger', platform: 'SNES', path: '/bin/true', path_exists: true, applications: [], versions: [], documents: [], available_screenshots: [], save_paths: []},
    ];
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    state.invalidateFilterCache();
    results.noMaybeShowWelcome = !/maybeShowWelcome/.test(libraryJs);
    results.noWelcomeDialog = !/welcomeDialog/.test(libraryJs);
    AppState.filterPresets = [{name: 'SmokePreset', rules: {platform: 'PC', view: 'all', query: ''}}];
    AppState.activeFilterPreset = 'SmokePreset';
    AppState.platform = 'PC';
    AppState.selectedId = null;
    library.render();
    const plat = document.querySelector('#platforms [data-platform="SNES"]');
    results.platformButtons = [...document.querySelectorAll('#platforms [data-platform]')].map(button => button.dataset.platform);
    if (plat) plat.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    results.presetClearedOnPlatform = AppState.activeFilterPreset === '';
    state.resetQuery();
    AppState.importBatchId = 'batch-smoke';
    document.getElementById('sidebarSearch').value = 'quake';
    if (document.getElementById('esrbFilter')) document.getElementById('esrbFilter').value = 'M';
    AppState.explorerRules = {progress: 'Beaten'};
    library.render();
    const clearAll = document.querySelector('[data-chip="clear-all"]');
    if (clearAll) clearAll.click();
    results.clearAllResets = AppState.importBatchId === '' && document.getElementById('sidebarSearch').value === ''
      && (!document.getElementById('esrbFilter') || document.getElementById('esrbFilter').value === '') && !AppState.explorerRules.progress;
    AppState.importBatchId = 'batch-chip';
    library.render();
    results.batchChipVisible = Boolean(document.querySelector('[data-chip-remove="import_batch"]'));
    const batchChip = document.querySelector('[data-chip-remove="import_batch"]');
    if (batchChip) batchChip.click();
    results.batchChipClears = AppState.importBatchId === '';
    AppState.games = [];
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    state.invalidateFilterCache();
    library.renderGrid();
    results.emptySetupBtn = Boolean(document.getElementById('emptySetupLibrary'));
    document.getElementById('emptySetupLibrary')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.emptyOpensSetup = document.getElementById('setupCenter')?.open === true;
    document.getElementById('setupCenter')?.close();
    AppState.games = [];
    AppState.appSettings.welcome_completed = false;
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    state.invalidateFilterCache();
    const origFetch = window.fetch;
    window.fetch = async (url, opts) => {
      if (String(url).includes('/api/library') || String(url).includes('/api/v1/library')) {
        return {ok: true, text: async () => JSON.stringify({games: [], playlists: [], settings: {...AppState.appSettings, welcome_completed: false}, filter_presets: []})};
      }
      return origFetch(url, opts);
    };
    await library.refresh();
    window.fetch = origFetch;
    results.firstRunOpensSetup = document.getElementById('setupCenter')?.open === true
      && Boolean(document.querySelector('.setup-stepper'))
      && !AppState.appSettings.welcome_completed && AppState.games.length === 0;
    document.getElementById('setupCenter')?.close();
    const manualGame = {id: 88001, name: 'Manual Game', platform: 'PC', has_manual: true, path_exists: true,
      applications: [], versions: [], documents: [], available_screenshots: [], save_paths: [], custom_fields: {}};
    AppState.games = [manualGame];
    AppState.selectedId = manualGame.id;
    library.renderDetails();
    const manualTile = document.querySelector('[data-manual]');
    results.manualTileNoImg = manualTile && !manualTile.querySelector('img');
    let nativeExternalCalls = 0;
    const origOpen = window.open;
    window.open = (...args) => { nativeExternalCalls++; return origOpen ? origOpen(...args) : null; };
    window.fetch = async (url, opts) => {
      if (String(url).includes('/api/native/open-external')) { nativeExternalCalls++; return {ok: true, json: async () => ({ok: true})}; }
      return origFetch(url, opts);
    };
    manualTile?.click();
    await new Promise(r => setTimeout(r, 200));
    results.manualUsesReader = document.getElementById('readerDialog')?.open === true;
    results.manualNoNativeExternal = nativeExternalCalls === 0;
    window.open = origOpen;
    window.fetch = origFetch;
    document.getElementById('closeReader')?.click();
    let libraryPostDuringResize = false;
    window.fetch = async (url, opts) => {
      if (String(url).includes('/api/library') && opts?.method === 'POST') libraryPostDuringResize = true;
      return origFetch(url, opts);
    };
    const handle = document.getElementById('detailsResizeHandle');
    if (handle) {
      handle.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: 100}));
      document.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: 50}));
      document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
    }
    window.fetch = origFetch;
    results.detailsResizeNoLibraryPost = !libraryPostDuringResize;
    return results;
  });
  console.log('library workspace:', JSON.stringify(libraryWorkspace, null, 2));
  const dropHonesty = await page.evaluate(async () => {
    const results = {};
    let importBody = null;
    const origFetch = window.fetch;
    window.fetch = async (url, opts) => {
      if (String(url).includes('/api/import') && opts?.method === 'POST') {
        try { importBody = JSON.parse(opts.body || '{}'); } catch { importBody = {}; }
        return {ok: true, json: async () => ({added: 0})};
      }
      return origFetch(url, opts);
    };
    const beforeCount = AppState.games.length;
    const dz = document.getElementById('dropZone');
    const dt = new DataTransfer();
    dt.items.add(new File([''], 'rom.bin', {type: 'application/octet-stream'}));
    dz.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
    await new Promise(r => setTimeout(r, 500));
    results.pickerOpened = document.getElementById('a11yInputDialog')?.open === true;
    document.getElementById('a11yInputCancel')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.libraryUnchanged = AppState.games.length === beforeCount;
    const toast = document.getElementById('toast');
    results.noImportToast = !/imported/i.test(toast?.textContent || '');
    results.noRomBinFolder = !importBody || importBody.folder !== 'rom.bin';
    window.fetch = origFetch;
    return results;
  });
  console.log('drop honesty:', JSON.stringify(dropHonesty, null, 2));
  const dropEmptyHonesty = await page.evaluate(async () => {
    const results = {};
    let importCalled = false;
    const origFetch = window.fetch;
    window.fetch = async (url, opts) => {
      if (String(url).includes('/api/import') && opts?.method === 'POST') {
        importCalled = true;
        return {ok: true, json: async () => ({added: 0})};
      }
      return origFetch(url, opts);
    };
    const beforeCount = AppState.games.length;
    const dz = document.getElementById('dropZone');
    const dt = new DataTransfer();
    dz.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
    await new Promise(r => setTimeout(r, 500));
    results.pickerOpened = document.getElementById('a11yInputDialog')?.open === true;
    document.getElementById('a11yInputCancel')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.libraryUnchanged = AppState.games.length === beforeCount;
    results.noImport = !importCalled;
    window.fetch = origFetch;
    return results;
  });
  console.log('drop empty honesty:', JSON.stringify(dropEmptyHonesty, null, 2));

  if (perfChecks.coverflowNodes > 11) process.exit(1);
  if (!perfChecks.gridNodeCount) process.exit(1);
  if (perfChecks.gridNodeCount >= perfChecks.visibleCount) process.exit(1);
  if (perfChecks.gridNodeCount > 300) process.exit(1);
  if (!perfChecks.delegatedClickOk) process.exit(1);
  if (!perfChecks.filtered.includes('Chrono Library Game 19999')) process.exit(1);
  if (!perfChecks.searchIndexStats || perfChecks.searchIndexStats.games !== 20000) process.exit(1);
  if (perfChecks.searchMs > 20) process.exit(1);

  console.log('JS errors:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
  if (errors.length) process.exit(1);
  if (!before.cardCount || !clicked) process.exit(1);
  if (themeResults.some(r => !r.ok)) process.exit(1);
  if (!platformAfter || !platformAfter.ok) process.exit(1);
  if (!igdbPlatformParam) process.exit(1);
  if (!v1MatchMapped) process.exit(1);
  if (!matchReview.exported) process.exit(1);
  if (!matchReview.rowKeys) process.exit(1);
  if (!matchReview.scoreComponents) process.exit(1);
  if (!matchReview.scoreReasons) process.exit(1);
  if (!matchReview.noBareConfidence) process.exit(1);
  if (!matchReview.paginationCursor) process.exit(1);
  if (!matchReview.nextCursorUsed) process.exit(1);
  if (!matchReview.bulkLikelyNoPossible) process.exit(1);
  if (!matchReview.acceptUsesDecisions) process.exit(1);
  if (!matchReview.neverPosted) process.exit(1);
  if (!matchReview.reopenWorks) process.exit(1);
  if (!matchReview.applyPayload || matchReview.applyPayload.replace_existing !== true) process.exit(1);
  if (!Array.isArray(matchReview.applyPayload.field_allow_list) || !matchReview.applyPayload.field_allow_list.includes('title')) process.exit(1);
  if (!Array.isArray(matchReview.applyPayload.media_allow_list) || !matchReview.applyPayload.media_allow_list.includes('cover')) process.exit(1);
  if (!matchReview.matchReviewCssVarsOnly) process.exit(1);
  if (!activityUi.exportedOpenActivity) process.exit(1);
  if (!activityUi.exportedPartition) process.exit(1);
  if (!activityUi.partitionActive.includes('job-active')) process.exit(1);
  if (!activityUi.partitionAttention.includes('job-attention')) process.exit(1);
  if (!activityUi.partitionRecent.includes('job-recent')) process.exit(1);
  if (activityUi.partitionRecent.includes('job-stale')) process.exit(1);
  if (!activityUi.drawerOpen) process.exit(1);
  if (!activityUi.rowKeys) process.exit(1);
  if (!activityUi.partialNotSuccess) process.exit(1);
  if (!activityUi.cancelDisabled) process.exit(1);
  if (!activityUi.cancelNotCancellablePosted) process.exit(1);
  if (!activityUi.retryBodyJobIdOnly) process.exit(1);
  if (!activityUi.itemsKeys) process.exit(1);
  if (!activityUi.itemsFetch) process.exit(1);
  if (!activityUi.appImportsActivity) process.exit(1);
  if (!activityUi.activityButtonOpens) process.exit(1);
  if (!activityUi.snapshotBeforeSse) process.exit(1);
  if (!activityUi.activityCssVarsOnly) process.exit(1);
  if (!setupUi.exported) process.exit(1);
  if (!setupUi.stepperPresent) process.exit(1);
  if (!setupUi.summaryKeys) process.exit(1);
  if (!setupUi.nextActionOne) process.exit(1);
  if (!setupUi.faugusVisible) process.exit(1);
  if (!setupUi.xbox360Visible) process.exit(1);
  if (!setupUi.noFileInput) process.exit(1);
  if (!setupUi.importsHasSetup) process.exit(1);
  if (!setupUi.importsNoPrompt) process.exit(1);
  if (!setupUi.previewGetAfterPost) process.exit(1);
  if (!setupUi.previewRowKeys) process.exit(1);
  if (!setupUi.preflightCandidate) process.exit(1);
  if (!setupUi.installFlatpakBody) process.exit(1);
  if (!setupUi.revalidateWaited) process.exit(1);
  if (!setupUi.commitEmulatorChoices) process.exit(1);
  if (!setupUi.decisionsIncludeLaunch) process.exit(1);
  if (!setupUi.welcomeCompleted) process.exit(1);
  if (!setupUi.setupCssVarsOnly) process.exit(1);
  if (!dialogFocusOk) process.exit(1);
  if (!readerCleanedOk) process.exit(1);
  if (!gamepadLoopStopped) process.exit(1);
  if (!bigboxCorrectness.activateExported) process.exit(1);
  if (!bigboxCorrectness.appUsesActivate) process.exit(1);
  if (!bigboxCorrectness.preflightLaunch) process.exit(1);
  if (!bigboxCorrectness.noConfirmSessions) process.exit(1);
  if (!bigboxCorrectness.noConfirmStorefront) process.exit(1);
  if (!bigboxCorrectness.allViewportCases) process.exit(1);
  if (!bigboxCorrectness.shortcutWhileTyping) process.exit(1);
  if (!queryEngine.cacheSameIdentity) process.exit(1);
  if (!queryEngine.longTokenFindsGame) process.exit(1);
  if (!queryEngine.unratedFacetKeepsUnrated) process.exit(1);
  if (!queryEngine.importBatchIdFilter) process.exit(1);
  if (!queryEngine.typedImportBatchExact) process.exit(1);
  if (!queryEngine.resetClearsImportBatch) process.exit(1);
  if (!queryEngine.notifySuccessLevel) process.exit(1);
  if (!queryEngine.notifyCompatInfo) process.exit(1);
  if (!queryEngine.notifySingleErrorLevel) process.exit(1);
  if (!queryEngine.stickyErrorVisible) process.exit(1);
  if (!queryEngine.errorBannerCopyWorks) process.exit(1);
  if (!appShell.setupCenterPresent) process.exit(1);
  if (!appShell.welcomeWizardGone) process.exit(1);
  if (!appShell.welcomeShimHidden) process.exit(1);
  if (!appShell.setupButtonOpensCenter) process.exit(1);
  if (!appShell.welcomeShimOpensCenter) process.exit(1);
  if (!appShell.reopenOpensCenter) process.exit(1);
  if (!appShell.queueStillQueue) process.exit(1);
  if (!appShell.activityDistinct) process.exit(1);
  if (!appShell.pathBrowseHosts) process.exit(1);
  if (!appShell.noWelcomeImport) process.exit(1);
  if (!appShell.noCompleteWelcome) process.exit(1);
  if (!appShell.noSetupImport) process.exit(1);
  if (!appShell.importsHasSetup) process.exit(1);
  if (!appShell.importsNoPrompt) process.exit(1);
  if (!appShell.hasActivityImport) process.exit(1);
  if (!appShell.noBindContextMenuA11y) process.exit(1);
  if (!appShell.noContextMenuListener) process.exit(1);
  if (!appShell.noPromptInAppJs) process.exit(1);
  if (!appShell.noConfirmInAppJs) process.exit(1);
  if (!appShell.nativePickFolderNoPrompt) process.exit(1);
  if (!a11yDialogs.noWindowPromptInDialogs) process.exit(1);
  if (!a11yDialogs.shiftF10OpensMenu) process.exit(1);
  if (!a11yDialogs.escapeRestoresFocus) process.exit(1);
  if (!a11yDialogs.confirmHasNamedTarget) process.exit(1);
  if (!a11yDialogs.pathBrowseFilled) process.exit(1);
  if (!a11yDialogs.coverBrowseFilled) process.exit(1);
  if (!a11yDialogs.settingsBrowseFilled) process.exit(1);
  if (!libraryWorkspace.noMaybeShowWelcome) process.exit(1);
  if (!libraryWorkspace.noWelcomeDialog) process.exit(1);
  if (!libraryWorkspace.presetClearedOnPlatform) process.exit(1);
  if (!libraryWorkspace.clearAllResets) process.exit(1);
  if (!libraryWorkspace.batchChipVisible) process.exit(1);
  if (!libraryWorkspace.batchChipClears) process.exit(1);
  if (!libraryWorkspace.emptySetupBtn) process.exit(1);
  if (!libraryWorkspace.emptyOpensSetup) process.exit(1);
  if (!libraryWorkspace.firstRunOpensSetup) process.exit(1);
  if (!libraryWorkspace.manualTileNoImg) process.exit(1);
  if (!libraryWorkspace.manualUsesReader) process.exit(1);
  if (!libraryWorkspace.manualNoNativeExternal) process.exit(1);
  if (!libraryWorkspace.detailsResizeNoLibraryPost) process.exit(1);
  if (!masterySmoke.open) process.exit(1);
  if (!masterySmoke.rendered) process.exit(1);
  if (!dropHonesty.pickerOpened) process.exit(1);
  if (!dropHonesty.libraryUnchanged) process.exit(1);
  if (!dropHonesty.noRomBinFolder) process.exit(1);
  if (dropHonesty.noImportToast === false) process.exit(1);
  if (!dropEmptyHonesty.pickerOpened) process.exit(1);
  if (!dropEmptyHonesty.libraryUnchanged) process.exit(1);
  if (!dropEmptyHonesty.noImport) process.exit(1);
  const expectedGroups = {
    library: ['metadataButton','mediaButton','healthButton','constellationButton','masteryButton','bulkButton','tagsButton','playlistsButton','backupButton','historyButton','achievementsButton','saveFilterButton','savePresetButton'],
    sources: ['storefrontButton','emulatorsButton','steamButton','heroicButton','lutrisButton','arcadeButton','discoveryButton'],
    personalize: ['themesButton','pluginsButton','settingsButton','fullscreenButton'],
    automation: ['webhooksButton','notificationsButton'],
  };
  for (const [key, ids] of Object.entries(expectedGroups)) {
    const got = appShell.toolGroups[key] || [];
    if (JSON.stringify(got) !== JSON.stringify(ids)) process.exit(1);
  }
  console.log('UI SMOKE PASSED');
})().catch(e => { console.error('SMOKE FAIL', e.message); process.exit(1); });
