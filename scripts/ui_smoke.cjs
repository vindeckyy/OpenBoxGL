const fs = require('fs');
const puppeteer = require('./node_modules/puppeteer');

// Each expectation below records into `failures` rather than exiting on the spot,
// so one run reports every failed check by name instead of stopping at the first
// one with a bare exit code.
const failures = [];

(async () => {
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined);
  const launchOpts = {headless: 'new', args: ['--no-sandbox']};
  if (executablePath) launchOpts.executablePath = executablePath;
  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  // Pin reduced motion for the functional flows: headless Chrome inherits the OS setting, so leaving it unset makes\r\n  // dialog closes synchronous on Windows and animated on Linux. The motion block below turns real motion on explicitly.
  await page.emulateMediaFeatures([{name: 'prefers-reduced-motion', value: 'reduce'}]);
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
  // Job timestamps are offsets from now, not pinned calendar dates. The recent
  // partition is a rolling 30-day window (RECENT_MS in static/activity.js), so
  // a literal date silently ages out and the smoke test starts failing months
  // later for a reason that has nothing to do with the code under test.
  const DAY_MS = 24 * 60 * 60 * 1000;
  const activityNow = Date.now();
  const ago = (days) => new Date(activityNow - days * DAY_MS).toISOString();
  const activityFixture = {
    now: activityNow,
    jobs: [
      {
        job_id: 'job-active', root_job_id: 'job-active', retry_of: null, resume_of: null,
        type: 'setup.scan', title: 'Library scan', state: 'running', phase: 'scan',
        current: 2, total: 10, message: 'Scanning folders',
        created_at: ago(2), updated_at: ago(2), started_at: ago(2), finished_at: null,
        can_cancel: true, can_retry: false, can_resume: false, input: {}, checkpoint: null, result: null, error: null,
      },
      {
        job_id: 'job-attention', root_job_id: 'job-attention', retry_of: null, resume_of: null,
        type: 'media.bulk_download', title: 'Media download', state: 'partial', phase: 'download',
        current: 3, total: 5, message: 'Some downloads failed',
        created_at: ago(3), updated_at: ago(3), started_at: ago(3), finished_at: ago(3),
        can_cancel: false, can_retry: true, can_resume: false, input: {}, checkpoint: {failed_game_ids: ['g1']}, result: null, error: null,
      },
      {
        job_id: 'job-recent', root_job_id: 'job-recent', retry_of: null, resume_of: null,
        type: 'library.backup', title: 'Library backup', state: 'done', phase: 'complete',
        current: 1, total: 1, message: 'Backup complete',
        created_at: ago(2), updated_at: ago(2), started_at: ago(2), finished_at: ago(2),
        can_cancel: false, can_retry: false, can_resume: false, input: {}, checkpoint: null, result: {path: '/tmp/backup.zip'}, error: null,
      },
      {
        job_id: 'job-stale', root_job_id: 'job-stale', retry_of: null, resume_of: null,
        type: 'cloud.sync', title: 'Cloud sync', state: 'error', phase: 'sync',
        current: 0, total: 1, message: 'Auth failed',
        created_at: ago(120), updated_at: ago(120), started_at: ago(120), finished_at: ago(120),
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
      if (href.includes('/api/v2/metadata/auto-scrape') && method === 'POST') {
        calls.finishOrder.push('auto_scrape');
        return jsonResponse({queued: false});
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

  // F18b Setup Center behavior pins (1.13.1 §2.1): source cards, stepper,
  // empty scan/finish states, dismissal, insights, and Big Box empty view.
  // A query-string import gives this block its own setup.js instance/state.
  const setupBehavior = await page.evaluate(async () => {
    const results = {};
    const tick = (ms = 150) => new Promise(r => setTimeout(r, ms));
    const origFetch = window.fetch;
    const jsonResponse = (body, status = 200) => {
      const text = JSON.stringify(body);
      return {ok: status >= 200 && status < 300, status, text: async () => text, json: async () => body};
    };
    const summaryFixture = {
      library_count: 0, source_coverage: [], metadata_match_percent: 0, media_gaps: 0,
      duplicate_count: 0, missing_paths: 0,
      emulator_readiness: {ready: 0, warning: 0, blocked: 0, unknown: 0},
      active_operations: 0, next_action: {id: 'add_sources', label: 'Add game sources', step: 2},
    };
    window.fetch = async (url, opts = {}) => {
      const href = String(url);
      const method = (opts.method || 'GET').toUpperCase();
      if (href.includes('/api/v2/setup/summary') && method === 'GET') return jsonResponse(summaryFixture);
      if (href.includes('/api/v2/setup/preview/revalidate') && method === 'POST')
        return jsonResponse({preview_id: 'p-empty', revision: 2, job_id: null, state: 'queued'});
      if (href.includes('/api/v2/setup/preview') && method === 'POST' && !href.includes('/preview/'))
        return jsonResponse({preview_id: 'p-empty', revision: 1, job_id: null, state: 'queued'}, 202);
      if (href.includes('/api/v2/setup/preview?') && method === 'GET')
        return jsonResponse({preview_id: 'p-empty', revision: 1, state: 'ready', revalidated: true, scanned_entries: 0, counts: {additions: 0, merges: 0}});
      if (href.includes('/api/v2/setup/preview/items') && method === 'GET')
        return jsonResponse({preview_id: 'p-empty', revision: 1, items: [], next_cursor: null});
      if (href.includes('/api/v2/setup/commit') && method === 'POST')
        return jsonResponse({preview_id: 'p-empty', revision: 2, job_id: 'job-commit-empty', import_batch_id: ''}, 202);
      if (href.includes('/api/v2/jobs'))
        return jsonResponse({jobs: [{job_id: 'job-commit-empty', state: 'done', type: 'setup.commit', result: {added: 0, merged: 0, skipped: 4}, can_cancel: false, can_retry: false, can_resume: false}], next_cursor: null});
      if (href.includes('/api/library') || href.includes('/api/v1/library'))
        return jsonResponse({games: [], playlists: [], settings: {locale: 'en'}, filter_presets: [], media_epoch: 0});
      if (href.includes('/api/settings') && method === 'POST') {
        const body = opts.body ? JSON.parse(opts.body) : {};
        return jsonResponse(body);
      }
      if (href.includes('/api/v2/insights/summary'))
        return jsonResponse({heatmap: [], totals: {}, top_platforms: [], top_genres: [], top_games: [], streak: null, momentum: null});
      if (href.includes('/api/v2/insights/radio') || href.includes('/api/v2/insights/radar'))
        return jsonResponse({});
      return origFetch(url, opts);
    };
    const mod = await import('/static/setup.js?f18b=' + Date.now());
    const libraryMod = await import('/static/library.js');
    const insightsMod = await import('/static/insights.js');
    const savedGames = AppState.games;
    const savedBigBoxGames = AppState.bigBoxGames;
    AppState.appSettings.welcome_completed = false;
    AppState.setupDismissed = false;

    // --- sources (step 2) ---
    await mod.openSetupCenter({step: 2});
    await tick();
    const icons = [...document.querySelectorAll('.setup-source-icon')];
    results.sourceIconsMonogram = icons.length > 0 && icons.every(el => /^[A-Za-z0-9]{1,2}$/.test(el.textContent.trim()));
    document.querySelector('[data-source-key="steam"]')?.click();
    await tick();
    results.selectedSourceCard = Boolean(document.querySelector('[data-source-key="steam"].selected'))
      && document.querySelectorAll('.setup-source-selected li').length === 1;
    document.querySelector('[data-source-key="steam"]')?.click();
    await tick();
    results.duplicateSourceBlocked = document.querySelectorAll('.setup-source-selected li').length === 1;
    document.querySelector('[data-remove-source]')?.click();
    await tick();
    results.removeSelectedSource = document.querySelectorAll('.setup-source-selected li').length === 0
      && !document.querySelector('[data-source-key="steam"].selected');
    results.onlyCompletedStepsClickable = [...document.querySelectorAll('.setup-step-item')]
      .every(item => (Number(item.dataset.setupStep) < 2) === Boolean(item.onclick));
    const details = document.querySelector('.setup-more-sources');
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    document.querySelector('[data-source-key="faugus"]')?.click();
    await tick();
    results.moreSourcesSurvivesRerender = document.querySelector('.setup-more-sources')?.open === true;

    // --- empty scan (step 2 -> 3) ---
    document.getElementById('setupContinue')?.click();
    await tick(900);
    const emptyMsg = document.querySelector('.setup-preview-list .setup-empty');
    results.emptyScanMessage = Boolean(emptyMsg) && /no importable games/i.test(emptyMsg.textContent);

    // --- dismiss via close button, then library refresh: no reopen ---
    document.getElementById('closeSetupCenter')?.click();
    await tick();
    results.dismissedFlag = AppState.setupDismissed === true;
    await libraryMod.refresh();
    await tick();
    results.dismissNoReopen = document.getElementById('setupCenter').open !== true;
    results.insightsHiddenOnEmpty = document.getElementById('insightsPanel')?.hidden === true;

    // --- zero-import finish (drive steps 3 -> 8) ---
    AppState.setupDismissed = true; // keep refresh() inside the finish pipeline from reopening the wizard
    await mod.openSetupCenter({step: 3});
    await tick();
    for (let i = 0; i < 5; i++) {
      document.getElementById('setupContinue')?.click();
      await tick(900);
    }
    await tick(1500);
    const finishPanel = document.querySelector('[data-setup-panel="finish"]');
    results.zeroImportHidesViewImported = Boolean(finishPanel) && !document.getElementById('setupViewImported');
    results.zeroImportMessage = Boolean(finishPanel) && /No games were imported/.test(finishPanel.textContent);

    // --- reopening the wizard refreshes step/state ---
    await mod.openSetupCenter({step: 2});
    await tick();
    await mod.openSetupCenter({step: 1});
    await tick();
    results.reopenRefreshesStep = document.querySelector('.setup-step-item.active')?.dataset.setupStep === '1'
      && document.getElementById('setupCenter').open === true;
    document.getElementById('setupCenter')?.close();

    // --- Big Box empty view explains and opens the wizard ---
    const bigboxMod = await import('/static/bigbox.js');
    AppState.games = [];
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    (await import('/static/state.js')).invalidateFilterCache();
    bigboxMod.openBigBox();
    await tick();
    results.bigboxEmptyExplains = (document.getElementById('toasts')?.textContent || '').length > 20;
    results.bigboxEmptyOpensWizard = document.getElementById('setupCenter').open === true
      && document.getElementById('bigBox').hidden === true;
    document.getElementById('setupCenter')?.close();

    // --- insights collapse persists via localStorage ---
    const panel = document.getElementById('insightsPanel');
    panel.hidden = false;
    localStorage.setItem('openbox-insights-collapsed', '1');
    insightsMod.bindInsights();
    document.getElementById('insightsBody')?.remove();
    await insightsMod.loadInsights();
    await tick(600);
    results.insightsCollapsePersists = document.getElementById('insightsBody')?.hidden === true
      && document.getElementById('insightsToggle')?.textContent === '▸';
    document.getElementById('insightsToggle')?.click();
    await tick();
    results.insightsToggleWritesStorage = localStorage.getItem('openbox-insights-collapsed') === '0'
      && document.getElementById('insightsBody')?.hidden === false;
    localStorage.setItem('openbox-insights-collapsed', '0'); // leave clean

    AppState.appSettings = {...AppState.appSettings, locale: 'en', welcome_completed: false};
    AppState.games = savedGames; // the gamepad/party sections below need the real library
    AppState.bigBoxGames = savedBigBoxGames;
    // Restoring games must invalidate the filter cache: the bigbox empty-view
    // check above emptied AppState.games and invalidated, so without this the
    // gamepad/party sections below would see a stale empty filteredGames().
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    (await import('/static/state.js')).invalidateFilterCache();
    window.fetch = origFetch;
    return results;
  });
  console.log('setup behavior:', JSON.stringify(setupBehavior, null, 2));

  // 6. Dialog focus management test
  const dialogFocusOk = await page.evaluate(async () => {
    const focusCalls = [];
    const RealFocus = HTMLElement.prototype.focus;
    HTMLElement.prototype.focus = function (...args) {
      focusCalls.push(this.id || this.tagName || '?');
      return RealFocus.apply(this, args);
    };
    const addBtn = document.getElementById('addButton');
    addBtn.focus();
    addBtn.click();
    const gameDialog = document.getElementById('gameDialog');
    // opening awaits ensureProfiles() (an API call); a fixed 300ms is too short on a slow loopback
    for (let i = 0; i < 40 && !gameDialog?.open; i++) await new Promise(r => setTimeout(r, 50));
    if (!gameDialog?.open) return {ok: false, why: 'gameDialog never opened'};
    const focusedOnOpen = document.activeElement;
    const openCalls = focusCalls.slice();
    document.getElementById('closeDialog').click();
    await new Promise(r => setTimeout(r, 400));
    const active = document.activeElement;
    const nested = [...document.querySelectorAll('dialog[open]')].map(d => d.id);
    HTMLElement.prototype.focus = RealFocus;
    return {
      ok: active === addBtn,
      why: active === addBtn ? '' : `focus on ${active?.id || active?.tagName || 'nothing'}`,
      stillOpen: !!gameDialog.open,
      nested,
      activeId: active?.id || '',
      addBtnConnected: addBtn.isConnected,
      addBtnRendered: addBtn.getClientRects().length > 0,
      addBtnDisabled: addBtn.disabled,
      addBtnTabIndex: addBtn.tabIndex,
      focusedOnOpen: focusedOnOpen.id || focusedOnOpen.tagName,
      openCalls,
      closeCalls: focusCalls.slice(openCalls.length),
    };
  });
  console.log('dialog focus restore:', JSON.stringify(dialogFocusOk));

  // 6b. G-D2: click-outside on a nested dialog closes only the topmost dialog
  // (routed through closeDialog) and restores focus to a connected element.
  const nestedDialogOk = await page.evaluate(async () => {
    const dialogsMod = await import('/static/dialogs.js');
    const opener = document.getElementById('addButton');
    opener.focus();
    opener.click();
    await new Promise(r => setTimeout(r, 400));
    const gameDialog = document.getElementById('gameDialog');
    if (!gameDialog?.open) return {gameOpen: false};
    // Lazily created (body-appended) nested dialog, same path as
    // promptChoice/confirmAction.
    const choice = dialogsMod.promptChoice({title: 'Smoke', message: 'Pick one', choices: [{value: 'a', label: 'A'}]});
    await new Promise(r => setTimeout(r, 400));
    const nested = document.getElementById('a11yChoiceDialog');
    if (!nested?.open) { dialogsMod.closeDialog(gameDialog); return {nestedOpen: false}; }
    // The nested dialog autofocuses its <select>; the click-outside handler
    // ignores mousedowns while a SELECT is focused, so move focus first.
    nested.querySelector('button')?.focus();
    const rect = nested.getBoundingClientRect();
    document.dispatchEvent(new MouseEvent('mousedown', {
      bubbles: true, cancelable: true, clientX: rect.left - 50, clientY: rect.top - 50,
    }));
    await new Promise(r => setTimeout(r, 400));
    const out = {
      nestedClosed: !nested.open,
      gameStillOpen: gameDialog.open,
      focusRestored: document.activeElement !== document.body && Boolean(document.activeElement?.isConnected),
    };
    choice.then(() => {}, () => {});
    if (gameDialog.open) dialogsMod.closeDialog(gameDialog);
    return out;
  });
  console.log('nested dialog click-outside:', nestedDialogOk);

  // 7. Reader dialog cleanup test
  const readerCleanedOk = await page.evaluate(async () => {
    const readerMod = await import('/static/reader.js');
    const fakeGame = {id: 1, documents: [{name: 'Manual', path: '/tmp/test.pdf'}]};
    readerMod.openReader(fakeGame, 0);
    const frame = document.getElementById('readerFrame');
    // openReader navigates the frame via location.replace (no src attribute);
    // readerUrl being populated is the observable "document loaded" signal.
    const loadedBefore = AppState.readerUrl !== '';
    document.getElementById('closeReader').click();
    const hasSrcAfter = Boolean(frame.getAttribute('src'));
    const urlCleared = AppState.readerUrl === '';
    return loadedBefore && !hasSrcAfter && urlCleared;
  });
  console.log('reader cleanup:', {readerCleanedOk});
  const gamepadLoopStopped = await page.evaluate(async () => {
    const bigboxMod = await import('/static/bigbox.js');
    const {AppState} = await import('/static/state.js');
    AppState.bigBoxPlatform = 'all'; // the platform test above left a specific platform selected
    bigboxMod.openBigBox();
    const openVisible = !document.getElementById('bigBox').hidden;
    bigboxMod.closeBigBox();
    const closedVisible = document.getElementById('bigBox').hidden;
    return openVisible && closedVisible;
  });
  console.log('gamepad loop lifecycle:', {gamepadLoopStopped});

  // G-P1: Big Box pause overlay owns the gamepad while open — the pause
  // button opens it, d-pad moves focus between its buttons, and B dismisses
  // only the overlay (Big Box itself stays open).
  const gamepadPauseTrap = await page.evaluate(async () => {
    const bigboxMod = await import('/static/bigbox.js');
    const {AppState} = await import('/static/state.js');
    const {defaultControllerMap} = await import('/static/util.js');
    const mapping = {...defaultControllerMap, ...(AppState.appSettings.controller_map || {})};
    const result = {dismissControl: false, opened: false, focusMoved: false, dismissed: false, bigBoxAlive: false};
    result.dismissControl = Boolean(document.getElementById('closeBigBoxPause'));
    // Fake gamepad: the unified loop polls navigator.getGamepads() every frame.
    const pad = {id: 'smoke-pad', connected: true, axes: [0, 0, 0, 0],
      buttons: Array.from({length: 17}, () => ({pressed: false}))};
    Object.defineProperty(navigator, 'getGamepads', {value: () => [pad], configurable: true});
    const press = async index => {
      pad.buttons[index].pressed = true;
      await new Promise(r => setTimeout(r, 150));
      pad.buttons[index].pressed = false;
      await new Promise(r => setTimeout(r, 150));
    };
    try {
      bigboxMod.openBigBox();
      await new Promise(r => setTimeout(r, 200));
      // Seed a running session so the pause overlay has something to show.
      // pollSessions() can overwrite AppState.runningGames from the server at
      // any moment, so (re)seed right before each attempt and retry.
      const game = AppState.games[0];
      const seedRunning = () => {
        AppState.runningGames = [{game_id: game.id, game: game.name, launch_id: 'smoke-pause', paused: false, started: '2026-09-22T12:00:00'}];
      };
      for (let attempt = 0; attempt < 3 && !result.opened; attempt++) {
        seedRunning();
        await press(mapping.pause); // gamepad opens pause
        result.opened = !document.getElementById('bigBoxPause').hidden;
      }
      const first = document.activeElement;
      await press(13); // d-pad down moves focus
      const buttons = [...document.querySelectorAll('#bigBoxPauseActions button')];
      result.focusMoved = buttons.includes(document.activeElement) && document.activeElement !== first;
      await press(mapping.back); // B dismisses ONLY the overlay
      result.dismissed = document.getElementById('bigBoxPause').hidden;
      result.bigBoxAlive = !document.getElementById('bigBox').hidden;
    } finally {
      delete navigator.getGamepads;
      AppState.runningGames = [];
      bigboxMod.closeBigBox();
    }
    return result;
  });
  console.log('gamepad pause trap:', gamepadPauseTrap);

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

  // M6 Game Night: overlay opens from the Big Box menu, builds a wheel, Escape closes.
  // Seed two couch-eligible games server-side (node owns /tmp fixture files;
  // the page cannot create them). Cleaned up right after this block.
  const partySeedDir = fs.mkdtempSync(require('path').join(require('os').tmpdir(), 'obx-party-seed-'));
  const partySeedIds = [];
  try {
    for (const [name, rom] of [['Party Kart', 'kart.bin'], ['Couch Brawler', 'brawl.bin']]) {
      const romPath = `${partySeedDir}/${rom}`;
      fs.writeFileSync(romPath, 'party-smoke-fixture');
      const res = await fetch(`http://127.0.0.1:${process.env.PORT}/api/game`, {
        method: 'POST',
        headers: {'X-OpenBox-Token': process.env.TOKEN, 'Content-Type': 'application/json'},
        body: JSON.stringify({game: {name, platform: 'SNES', path: romPath, max_players: 4}}),
      });
      if (!res.ok) throw new Error(`party seed failed for ${name}: ${res.status}`);
    }
    const libRes = await fetch(`http://127.0.0.1:${process.env.PORT}/api/library`, {
      headers: {'X-OpenBox-Token': process.env.TOKEN},
    });
    const lib = await libRes.json();
    for (const g of lib.games || []) {
      if (String(g.name || '').startsWith('Party ') || g.name === 'Couch Brawler') partySeedIds.push(g.id);
    }
  } catch (error) {
    console.log('party seed failed:', error.message);
  }
  const partySmoke = await page.evaluate(async () => {
    const bigboxMod = await import('/static/bigbox.js');
    const libraryMod = await import('/static/library.js');
    // Earlier smoke stages replace AppState.games with sparse fixtures;
    // reload the normalized server library (seeded Quake + Chrono Trigger).
    await libraryMod.refresh();
    // Reset any library filters left over by earlier smoke stages.
    AppState.platform = 'all';
    AppState.platformCategory = 'all';
    AppState.activePlaylist = '';
    AppState.activeFilterPreset = '';
    AppState.importBatchId = '';
    AppState.explorerRules = {};
    AppState.bigBoxPlatform = 'all';
    const sidebarSearch = document.getElementById('sidebarSearch');
    if (sidebarSearch) sidebarSearch.value = '';
    const viewSel = document.getElementById('view');
    if (viewSel) viewSel.value = 'all';
    const esrbSel = document.getElementById('esrbFilter');
    if (esrbSel) esrbSel.value = '';
    bigboxMod.openBigBox();
    await new Promise(r => setTimeout(r, 300));
    if (document.getElementById('bigBox').hidden) return {bigBox: false};
    bigboxMod.openBigBoxMenu();
    await new Promise(r => setTimeout(r, 200));
    document.getElementById('partyMenuButton')?.click();
    await new Promise(r => setTimeout(r, 300));
    const overlayOpen = !document.getElementById('partyOverlay')?.hidden;
    const setupOk = Boolean(document.getElementById('partyBuild'));
    document.getElementById('partyMore')?.click();
    await new Promise(r => setTimeout(r, 200));
    const playersShown = document.getElementById('partyPlayers')?.textContent;
    document.getElementById('partyBuild')?.click();
    let wheel = false;
    for (let i = 0; i < 25 && !wheel; i++) {
      await new Promise(r => setTimeout(r, 200));
      wheel = Boolean(document.getElementById('partyWheel')?.querySelector('.party-slice-label'));
    }
    const upNext = document.getElementById('partyUpNext')?.children.length || 0;
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    await new Promise(r => setTimeout(r, 200));
    const closedByKeyboard = document.getElementById('partyOverlay')?.hidden === true;
    const bigBoxStillOpen = !document.getElementById('bigBox')?.hidden;
    bigboxMod.closeBigBox();
    return {bigBox: true, overlayOpen, setupOk, playersShown, wheel, upNext, closedByKeyboard, bigBoxStillOpen};
  });
  console.log('party dialog:', partySmoke);
  for (const id of partySeedIds) {
    await fetch(`http://127.0.0.1:${process.env.PORT}/api/game/delete`, {
      method: 'POST',
      headers: {'X-OpenBox-Token': process.env.TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    }).catch(() => {});
  }
  fs.rmSync(partySeedDir, {recursive: true, force: true});

  // 1.9.0 regression (M3 fixes): the constellation canvas must paint
  // non-blank pixels with translated kind labels — canvas var() colors,
  // frozen module-level labels, unclamped layout steps, and a stuck
  // loading indicator each blanked it in turn.
  const constellationSmoke = await page.evaluate(async () => {
    const libraryMod = await import('/static/library.js');
    await libraryMod.refresh();
    const constMod = await import('/static/constellation.js');
    constMod.openConstellation();
    await new Promise(r => setTimeout(r, 500));
    const open = document.getElementById('constellationDialog')?.open === true;
    let rendered = false;
    for (let i = 0; i < 40 && !rendered; i++) {
      await new Promise(r => setTimeout(r, 200));
      const canvas = document.getElementById('constellationCanvas');
      const loadingDone = document.getElementById('constellationLoading')?.hidden === true;
      if (canvas && loadingDone) {
        const ctx = canvas.getContext('2d');
        const px = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        for (let p = 0; p < px.length; p += 400) {
          if (px[p] > 8 || px[p + 1] > 8 || px[p + 2] > 8) { rendered = true; break; }
        }
      }
    }
    const labels = [...document.querySelectorAll('#constellationKinds label')].map(l => l.textContent.trim());
    document.getElementById('closeConstellation')?.click();
    await new Promise(r => setTimeout(r, 200));
    const closed = document.getElementById('constellationDialog')?.open !== true;
    return {open, rendered, labels, closed};
  });
  console.log('constellation dialog:', JSON.stringify(constellationSmoke));

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
      (!caseOk) && failures.push(`big box ${mode} layout at ${viewport.width}x${viewport.height}`);
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
    // G2 (2 of 3): aria-setsize / aria-posinset, measured here because this is
    // the only point where the 20,000-game fixture is actually in view. A
    // screen reader announces "N of M"; M must be the whole result set, not the
    // virtualizer's handful of rendered nodes, and N must run 1..k without gaps
    // so that "item 4,000" is reachable by keyboard and screen reader.
    const cards = [...document.querySelectorAll('#grid .card[aria-posinset]')];
    const setsize = cards.length ? Number(cards[0].getAttribute('aria-setsize')) : 0;
    const positions = cards.map(card => Number(card.getAttribute('aria-posinset')));
    const listPosition = {
      checked: cards.length,
      setsize,
      visibleCount,
      first: positions[0] ?? null,
      last: positions.at(-1) ?? null,
      contiguous: positions.every((value, index) => value === index + 1),
      consistent: cards.every(card => Number(card.getAttribute('aria-setsize')) === setsize),
      // The whole point: M is the result count, not the rendered slice.
      coversWholeSet: setsize === visibleCount,
    };
    listPosition.ok = Boolean(
      listPosition.checked && listPosition.contiguous && listPosition.consistent
      && listPosition.coversWholeSet && setsize > cards.length
    );
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
    return {coverflowNodes, delegatedClickOk, gridNodeCount, visibleCount, listPosition, filtered: filtered.map(game => game.name).slice(0, 3), searchMs, searchIndexStats: AppState.searchIndexStats};
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
    // The toast is a stack now, not one element that gets overwritten, so each
    // assertion looks at the newest toast rather than at a single #toast.
    const newestToast = () => {
      const layer = document.getElementById('toasts');
      const all = layer ? [...layer.querySelectorAll('.toast')] : [];
      return all.at(-1) || null;
    };
    state.notify('success', 'ok-success');
    results.notifySuccessLevel = newestToast()?.dataset.notifyLevel === 'success';
    state.notify('Preset deleted');
    const info = newestToast();
    results.notifyCompatInfo = info?.dataset.notifyLevel === 'info' && info.textContent === 'Preset deleted';
    state.notify('error');
    results.notifySingleErrorLevel = newestToast()?.dataset.notifyLevel === 'error';
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
    // F14: opening the context menu now re-renders the grid, so the focused card
    // is a *new* element by the time Escape closes the menu. Comparing node
    // identity here would report a focus regression that did not happen -- the
    // user is still on the same game. Identity of the game, not of the node.
    const after = document.activeElement;
    const afterGameId = after?.closest?.('[data-game]')?.dataset.game ?? after?.dataset?.game ?? null;
    results.escapeRestoresFocus = after === beforeFocus || after === card
      || (afterGameId !== null && afterGameId === card.dataset.game);
    // The one check in this harness that failed on CI without reproducing: a dialog
    // left open routes Escape to itself, so record what state it actually saw.
    results.escapeRestoresFocusState = {
      openDialogs: [...document.querySelectorAll('dialog[open]')].map(dialog => dialog.id || dialog.tagName),
      menuStillOpen: document.getElementById('contextMenu')?.hidden === false,
      activeElement: document.activeElement?.id || document.activeElement?.className || document.activeElement?.tagName,
      afterGameId,
      beforeGameId: beforeFocus?.dataset?.game ?? null,
    };
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
    AppState.setupDismissed = false; // reset: the dismiss test above left this true
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
    // F8: the drop handler now ignores a transfer with no files, so record what
    // the synthetic DragEvent actually carried -- a bare DragEvent with no
    // dataTransfer would prove nothing either way.
    results.dropTypes = Array.from(dt.types || []);
    results.dropHasFiles = results.dropTypes.includes('Files');
    results.pickerOpened = document.getElementById('a11yInputDialog')?.open === true;
    document.getElementById('a11yInputCancel')?.click();
    await new Promise(r => setTimeout(r, 200));
    results.libraryUnchanged = AppState.games.length === beforeCount;
    results.noImport = !importCalled;
    window.fetch = origFetch;
    return results;
  });
  console.log('drop empty honesty:', JSON.stringify(dropEmptyHonesty, null, 2));

  // 14. Hardening rows: long names truncate, selected-game delete rebinds,
  // rapid filter switching stays consistent.
  const hardening = {};
  hardening.longName = await page.evaluate(async (token) => {
    const state = await (await fetch('/api/library', {headers: {'X-OpenBox-Token': token}})).json();
    AppState.games = state.games;
    if (!AppState.games.length) return {rendered: false};
    const name = 'A'.repeat(400);
    AppState.games[0].name = name;
    const search = document.getElementById('sidebarSearch');
    if (search) search.value = '';
    AppState.platform = 'all';
    AppState._refreshCounter = (AppState._refreshCounter || 0) + 1;
    document.getElementById('libraryButton').click();
    await new Promise(r => setTimeout(r, 600));
    const heading = [...document.querySelectorAll('.card h3')].find(h => h.textContent.startsWith(name));
    if (!heading) return {rendered: false};
    const style = getComputedStyle(heading);
    return {
      rendered: true,
      ellipsis: style.textOverflow === 'ellipsis',
      truncated: heading.scrollWidth > heading.clientWidth,
      pageOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  }, process.env.TOKEN);
  console.log('long name:', JSON.stringify(hardening.longName));

  // Delete the selected game with its details open: confirms resolve, the
  // selection clears, and the grid/details re-render without a stale rebind.
  hardening.deleteSelected = await page.evaluate(async () => {
    const card = document.querySelector('.card-main');
    if (!card) return {ok: false, reason: 'no card'};
    card.click();
    await new Promise(r => setTimeout(r, 600));
    const remove = document.getElementById('removeGameButton');
    if (!remove) return {ok: false, reason: 'details not open'};
    const before = AppState.games.length;
    remove.click();
    await new Promise(r => setTimeout(r, 400));
    document.getElementById('a11yConfirmOk').click();
    await new Promise(r => setTimeout(r, 500));
    document.getElementById('a11yConfirmOk').click();
    await new Promise(r => setTimeout(r, 1000));
    return {
      ok: true,
      selectedCleared: AppState.selectedId === null,
      countDropped: AppState.games.length === before - 1,
      staleDetails: document.getElementById('details').innerText.includes('Launch Doctor'),
    };
  });
  console.log('delete selected:', JSON.stringify(hardening.deleteSelected));

  hardening.filter = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll('#platforms [data-platform]')];
    if (!buttons.length) return {consistent: false, reason: 'no platform buttons'};
    for (let i = 0; i < 10; i++) buttons[i % buttons.length].click();
    return {clicked: true};
  });
  await new Promise(r => setTimeout(r, 800));
  const filterResult = await page.evaluate(() => {
    const first = filteredGames().map(g => g.id);
    const second = filteredGames().map(g => g.id);
    return {
      consistent: JSON.stringify(first) === JSON.stringify(second),
      cardsRendered: document.querySelectorAll('.card').length > 0,
    };
  });
  hardening.filter = {...hardening.filter, ...filterResult};
  console.log('rapid filter:', JSON.stringify(hardening.filter));

  // 15. Motion contract (ADR 0063): exits animate, reduced motion is instant, and data-driven
  // re-renders (a search keystroke) never replay the grid entrance.
  const motion = {};
  // Headless Chrome inherits the OS animation setting (Windows can report reduce), so pin it explicitly.
  await page.emulateMediaFeatures([{name: 'prefers-reduced-motion', value: 'no-preference'}]);
  motion.gridEntrance = await page.evaluate(async () => {
    const search = document.getElementById('sidebarSearch');
    search.value = '';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    await new Promise(r => setTimeout(r, 600));
    document.querySelectorAll('.motion-enter').forEach(el => el.classList.remove('motion-enter'));
    search.value = 'Chrono';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    await new Promise(r => setTimeout(r, 800));
    const replayed = document.querySelectorAll('.card.motion-enter,.list-row.motion-enter').length;
    search.value = '';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    return {replayed, cards: document.querySelectorAll('.card,.list-row').length};
  });
  console.log('motion grid entrance:', JSON.stringify(motion.gridEntrance));
  motion.dialogExit = await page.evaluate(async () => {
    const d = document.getElementById('themesDialog');
    d.showModal();
    await new Promise(r => setTimeout(r, 300));
    d.close();
    const during = d.open && d.classList.contains('closing');
    await new Promise(r => setTimeout(r, 500));
    return {during, after: !d.open && !d.classList.contains('closing')};
  });
  console.log('motion dialog exit:', JSON.stringify(motion.dialogExit));
  motion.toast = await page.evaluate(async () => {
    const st = await import('/static/state.js');
    // B2: the Undo toast and an unrelated message must coexist. The old
    // implementation had four writers on one #toast element and three of them
    // assigned its innerHTML, so this assert is the one that catches a live
    // "moved to trash" Undo being deleted by a trophy notification.
    const undoId = st.showToast({level: 'info', text: 'Moved', action: 'Undo', ms: 8000});
    st.notify('other message');
    st.notify('third message');
    await new Promise(r => setTimeout(r, 150));
    const layer = document.getElementById('toasts');
    const toasts = [...layer.querySelectorAll('.toast')];
    const kept = !!layer.querySelector('#toast-' + undoId + ' .toast-action');
    const topLayer = layer.matches(':popover-open');
    // B2: a real stack, not a one-deep pending slot.
    const stacked = toasts.length >= 3;
    // B3: hovering must stop the clock for the remaining time, not reset it.
    const undoToast = document.getElementById('toast-' + undoId);
    undoToast.dispatchEvent(new MouseEvent('mouseenter', {bubbles: false}));
    await new Promise(r => setTimeout(r, 300));
    const survived = !!document.getElementById('toast-' + undoId);
    undoToast.dispatchEvent(new MouseEvent('mouseleave', {bubbles: false}));
    st.hideToast();
    await new Promise(r => setTimeout(r, 600));
    return {kept, topLayer, stacked, survived, count: toasts.length};
  });
  console.log('motion toast:', JSON.stringify(motion.toast));

  // B5: a native <dialog> is top layer too, ordered by when it was shown, so a
  // toast opened first ends up underneath it and ::backdrop covers Undo. The
  // dialog opener re-shows the toast container; verify that actually happens.
  motion.toastAboveDialog = await page.evaluate(async () => {
    const st = await import('/static/state.js');
    const dlgs = await import('/static/dialogs.js');
    st.showToast({level: 'info', text: 'Raised', action: 'Undo', ms: 8000});
    await new Promise(r => setTimeout(r, 80));
    const layer = document.getElementById('toasts');
    const openedAt = performance.now();
    const d = document.createElement('dialog');
    d.innerHTML = '<p>x</p>';
    document.body.appendChild(d);
    d.showModal();
    await new Promise(r => setTimeout(r, 120));
    // Both are in the top layer; the toast must be the later of the two.
    const raised = layer.matches(':popover-open');
    d.close();
    d.remove();
    return {raised, ms: performance.now() - openedAt, hasDialogs: typeof dlgs.closeDialog === 'function'};
  });
  console.log('motion toast above dialog:', JSON.stringify(motion.toastAboveDialog));

  // G2 (1 of 3, planned in NEXT_UPDATE_PLAN-1.15 §5 and never written):
  // cumulative layout shift *after the app has settled*. A `buffered` observer
  // would replay every shift since page load -- including the deliberate
  // 20,000-game grid entrance and the virtualizer's own work -- which is the
  // opposite of what the claim is about. The claim is that steady-state UI
  // activity (a dialog, a toast, a filter) does not move content.
  motion.cls = await page.evaluate(async () => {
    let total = 0;
    let entries = 0;
    const sources = [];
    const observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        if (entry.hadRecentInput) continue;
        total += entry.value;
        entries += 1;
        for (const source of entry.sources || []) {
          sources.push(source.node ? (source.node.id || source.node.className || source.node.nodeName) : 'unknown');
        }
      }
    });
    // Not buffered: start counting from this point, after the page settled.
    observer.observe({type: 'layout-shift'});
    const st = await import('/static/state.js');
    const phases = {};
    const mark = async name => {
      const before = total;
      await new Promise(r => setTimeout(r, 350));
      phases[name] = total - before;
    };

    const dlg = document.getElementById('themesDialog');
    if (dlg) { dlg.showModal(); }
    await mark('dialog');
    if (dlg) { dlg.close(); }
    await mark('dialogClose');

    st.showToast({level: 'success', text: 'Layout shift probe', ms: 1200});
    await mark('toast');

    const search = document.getElementById('sidebarSearch');
    search.value = 'zzzz-no-match';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    await mark('searchEmpty');

    search.value = '';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    await mark('searchRestored');
    observer.disconnect();
    return {total, entries, phases, sources: [...new Set(sources)].slice(0, 6)};
  });
  console.log('motion cls:', JSON.stringify(motion.cls));

  // G2 (3 of 3): the theme stylesheet must have a real href on first paint, and
  // switching must cross-fade through the shared class rather than snapping.
  // Driven through loadTheme() -- the real path -- so the token, the theme name
  // and the cross-fade are all produced by the code under test, not by the test.
  motion.themePaint = await page.evaluate(async () => {
    const link = document.getElementById('themeStylesheet');
    const initialHref = link ? link.getAttribute('href') : null;
    // A stylesheet with no href applies nothing, so the app would render
    // unthemed while every "theme renders" check still passed.
    const applied = link ? link.sheet !== null : false;

    const settings = await import('/static/settings.js');
    const state = await import('/static/state.js');
    const listing = await settings.loadTheme(true);
    const themes = listing.themes || [];
    const current = listing.selected;
    const next = themes.find(name => name !== current) || current;
    const root = document.documentElement;
    let switched = false;
    const observer = new MutationObserver(() => {
      if (root.classList.contains('theme-switching')) switched = true;
    });
    observer.observe(root, {attributes: true, attributeFilter: ['class']});
    // state.api() attaches the session token; a bare fetch() gets a 403 and the
    // selection never changes, which is how this first attempt passed vacuously.
    if (next && next !== current) {
      await state.api('/api/themes/select', {method: 'POST', body: JSON.stringify({name: next})});
      await settings.loadTheme(true);
    }
    await new Promise(r => setTimeout(r, 800));
    observer.disconnect();
    return {
      hasHref: Boolean(initialHref),
      initialHref,
      applied,
      themeCount: themes.length,
      changed: Boolean(next) && next !== current,
      switched,
      settled: !root.classList.contains('theme-switching'),
      finalHref: link ? link.getAttribute('href') : null,
    };
  });
  console.log('motion theme paint:', JSON.stringify(motion.themePaint));
  await page.emulateMediaFeatures([{name: 'prefers-reduced-motion', value: 'reduce'}]);
  motion.reduced = await page.evaluate(async () => {
    const dur = getComputedStyle(document.documentElement).getPropertyValue('--dur-slow');
    const d = document.getElementById('themesDialog');
    d.showModal();
    await new Promise(r => setTimeout(r, 100));
    d.close();
    return {tokenZero: parseFloat(dur) <= 1, closedSync: !d.open, animations: document.getAnimations().filter(a => a.playState === 'running' && a.effect?.getTiming().iterations === 1 / 0 ? false : a.playState === 'running').length};
  });
  await page.emulateMediaFeatures([{name: 'prefers-reduced-motion', value: 'reduce'}]);
  console.log('motion reduced:', JSON.stringify(motion.reduced));

  // F2: a restore is not offerable until the diff loads, and a diff that fails
  // leaves no way to restore anyway. Driven through the real panel with a
  // stubbed transport so both arms are exercised against the shipped code --
  // the Python gate proves the structure, this proves the buttons appear and
  // disappear as the diff does.
  motion.restorePreview = await page.evaluate(async () => {    const RealFetch = window.fetch;
    const calls = {lists: 0, diffs: 0, restores: 0};
    let failNext = false;
    const list = {backups: [{
      name: 'OpenBoxBackup-2024-01-01-00-00-00-000000.zip',
      path: '/data/backups/OpenBoxBackup-2024-01-01-00-00-00-000000.zip',
      size: 4096, created: '2024-01-01T00:00:00', items: ['library', 'settings'], invalid: false,
    }]};
    const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
    const diff = {
      restore: {
        will_remove: {total: 2, truncated: false, rows: [
          {game_id: 'g-new-1', name: 'Added Since Backup'},
          {game_id: 'g-new-2', name: 'Also New'},
        ]},
        will_add: {total: 1, truncated: false, rows: [{game_id: 'g-old', name: 'Deleted Since Backup'}]},
        will_change: {total: 1, truncated: false, rows: [
          {game_id: 'g-edit', name: 'Edited Since Backup', fields: {progress: {from: 'Beaten', to: 'Playing'}}},
        ]},
      },
      settings: {restored: true, present: true, would_change: true, differs: true, redacted_secrets: true, keys_changed: ['locale']},
    };
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input?.url || '');
      // api() rewrites /api/* to the frozen v1 surface, so match the rewritten
      // paths, not the call-site spelling.
      if (/\/api\/v1\/backups?(\?|$)/.test(url)) { calls.lists += 1; return json(list); }
      if (url.includes('/backup/diff')) {
        calls.diffs += 1;
        return failNext ? json({error: 'archive unreadable'}, 500) : json(diff);
      }
      if (url.includes('/backup/restore')) { calls.restores += 1; return RealFetch(input, init); }
      return RealFetch(input, init);
    };
    const settings = await import('/static/settings.js');
    let openError = null;
    try { await settings.openBackups(); } catch (error) { openError = String(error && error.message || error); }
    await new Promise(r => setTimeout(r, 150));
    const panel = document.getElementById('backupPreview');
    const settle = () => new Promise(r => setTimeout(r, 150));
    const snapshot = () => ({
      listed: document.querySelectorAll('[data-restore-backup]').length,
      hidden: panel.hidden,
      confirm: !!panel.querySelector('[data-restore-confirm]'),
      retry: !!panel.querySelector('[data-restore-retry]'),
      names: [...panel.querySelectorAll('.timeline-name')].map(el => el.textContent.trim()),
      fields: [...panel.querySelectorAll('.tm-change')].map(el => el.textContent.replace(/\s+/g, ' ').trim()),
      text: panel.textContent.replace(/\s+/g, ' ').trim(),
    });
    // Nothing is armed before a diff has run.
    const before = snapshot();
    document.querySelector('[data-restore-backup]')?.click();
    await settle();
    const armed = snapshot();
    // A diff that fails must leave no confirm button at all.
    failNext = true;
    document.querySelector('[data-restore-backup]')?.click();
    await settle();
    const failed = snapshot();
    // And the retry goes back through the same gate, this time against a diff
    // that works.
    failNext = false;
    panel.querySelector('[data-restore-retry]')?.click();
    await settle();
    const recovered = snapshot();
    window.fetch = RealFetch;
    document.getElementById('backupDialog')?.close();
    panel.hidden = true;
    panel.innerHTML = '';
    return {before, armed, failed, recovered, calls, openError};
  });
  console.log('motion restore preview:', JSON.stringify(motion.restorePreview, null, 2));

  // F1: the Launch Readiness panel. The real route is hit once to prove it is
  // registered and never scans on GET; the panel itself is then driven against
  // a stubbed report so the groups, paging, the one-fix-per-cause button and
  // "Show me" are exercised deterministically on any fixture.
  motion.launchAudit = await page.evaluate(async () => {
    const RealFetch = window.fetch;
    const live = await RealFetch('/api/v2/launch/audit', {
      headers: {'X-OpenBox-Token': (await import('/static/state.js')).token},
    }).then(r => r.json().then(body => ({status: r.status, scanned: body.scanned}))).catch(error => ({error: String(error)}));
    const games = (await import('/static/state.js')).AppState.games;
    const target = games[0];
    const calls = {scans: 0, pages: 0};
    const json = body => new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}});
    const report = {
      scanned: true, computed_at: '2026-01-01T00:00:00+00:00', game_count: 30, stale: false, deep: false, failed: 0,
      totals: {ready: 4, warning: 6, blocked: 20},
      groups: [
        {key: 'FLATPAK_NOT_INSTALLED|flatpak_install|org.libretro.RetroArch', code: 'FLATPAK_NOT_INSTALLED', subject: 'org.libretro.RetroArch',
         severity: 'error', message: 'Emulator is not installed.', count: 20, fix_action: {kind: 'flatpak_install', payload: {app_id: 'org.libretro.RetroArch'}}},
        {key: 'SAVE_GAP||', code: 'SAVE_GAP', subject: '', severity: 'warning', message: 'No save paths configured.', count: 6, fix_action: null},
      ],
      job: null,
    };
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input?.url || '');
      if (url.includes('/launch/audit/scan')) { calls.scans += 1; return json({state: 'queued', job_id: 'job-1'}); }
      if (url.includes('/launch/audit')) {
        const group = new URL(url, location.origin).searchParams.get('group');
        if (!group) return json(report);
        calls.pages += 1;
        const offset = Number(new URL(url, location.origin).searchParams.get('offset') || 0);
        const members = Array.from({length: 20}, (_, i) => ({game_id: String(target?.game_id || ''), index: 0, name: `Member ${i}`}));
        return json({...report, members: {key: group, games: members.slice(offset, offset + 25), total: 20, offset, limit: 25}});
      }
      return RealFetch(input, init);
    };
    const settle = () => new Promise(r => setTimeout(r, 200));
    const health = await import('/static/health.js');
    await health.openHealthScore();
    await settle();
    const body = document.getElementById('launchAuditBody');
    const rows = [...body.querySelectorAll('[data-audit-group]')];
    const heads = rows.map(row => row.querySelector('.health-dim-name').textContent.trim());
    const installButtons = body.querySelectorAll('[data-audit-install]').length;
    const totals = body.querySelector('[role="status"]')?.textContent.trim() || '';
    rows[0]?.querySelector('[data-audit-toggle]')?.click();
    await settle();
    const members = rows[0]?.querySelectorAll('[data-audit-game]').length || 0;
    // "Show me" lands on the game and closes the dialog.
    rows[0]?.querySelector('[data-audit-game]')?.click();
    await settle();
    const dialogClosed = !document.getElementById('healthScoreDialog').open;
    const selected = (await import('/static/state.js')).AppState.selectedId;
    document.getElementById('launchAuditRun')?.click();
    await settle();
    const scansAfterRun = calls.scans;
    window.fetch = RealFetch;
    document.getElementById('healthScoreDialog')?.close();
    return {live, rows: rows.length, heads, installButtons, totals, members, pages: calls.pages, scansAfterRun,
            dialogClosed, selectedMatches: target ? selected === target.id : false, hadTarget: Boolean(target)};
  });
  console.log('motion launch audit:', JSON.stringify(motion.launchAudit, null, 2));

  // S18 regression, and the reason the restore-preview case above needed a
  // browser to find it. `inert` was only ever released by closeBigBox(); any
  // other path that hid #bigBox left the whole page inert. Nothing looks broken
  // -- buttons render and still take clicks -- but .focus() becomes a no-op, so
  // every dialog's focus restore silently lands on <body> and keyboard
  // navigation is dead for the rest of the session.
  motion.inertReleased = await page.evaluate(async () => {
    const bigbox = await import('/static/bigbox.js');
    const state = await import('/static/state.js');
    const inerted = () => [...document.body.children]
      .filter(el => el.hasAttribute('inert'))
      .map(el => el.id || el.tagName);
    const before = inerted();
    // A native modal makes the rest of the document inert by the browser's own
    // rules, so any dialog a previous block left open would confound every
    // reading below. Close them through the app's own choke point.
    const dialogs = await import('/static/dialogs.js');
    for (const d of [...document.querySelectorAll('dialog[open]')]) dialogs.closeDialog(d);
    await new Promise(r => setTimeout(r, 250));
    const clean = inerted();
    // Control measurement: if this probe cannot take focus even before Big Box
    // opens, the element is wrong for this point in the run and asserting on it
    // would report a failure that is not about inert.
    const probe = document.getElementById('addButton');
    probe.focus();
    const focusBefore = document.activeElement === probe;
    // openBigBox() defers to the wizard when the library is empty, and returns
    // before it ever sets inert -- so seed it or this case proves nothing. It
    // resets filter/sort/query but NOT the platform or RetroAchievements chips,
    // which earlier blocks in this run have set.
    const saved = {games: state.AppState.bigBoxGames, mode: state.AppState.appSettings.bigbox_mode,
      platform: state.AppState.bigBoxPlatform, ra: state.AppState.bigBoxRaFilter, query: document.getElementById('sidebarSearch').value};
    state.AppState.bigBoxPlatform = 'all';
    state.AppState.bigBoxRaFilter = '';
    document.getElementById('sidebarSearch').value = '';
    document.getElementById('sidebarSearch').dispatchEvent(new Event('input', {bubbles: true}));
    state.invalidateFilterCache();
    state.AppState.appSettings = {...state.AppState.appSettings, bigbox_mode: 'stage'};
    bigbox.openBigBox();
    await new Promise(r => setTimeout(r, 300));
    const opened = !document.getElementById('bigBox').hidden;
    const whileOpen = inerted();
    // Hide through a path that does not call closeBigBox().
    document.getElementById('bigBox').hidden = true;
    await new Promise(r => setTimeout(r, 300));
    const afterHide = inerted();
    probe.focus();
    const focusWorks = document.activeElement === probe;
    state.AppState.bigBoxGames = saved.games;
    state.AppState.appSettings = {...state.AppState.appSettings, bigbox_mode: saved.mode};
    state.AppState.bigBoxPlatform = saved.platform;
    state.AppState.bigBoxRaFilter = saved.ra;
    document.getElementById('sidebarSearch').value = saved.query;
    state.invalidateFilterCache();
    return {clean, opened, whileOpen, afterHide, focusBefore, focusWorks,
      gamesLen: state.filteredGames().length};
  });
  // F7/F11/F12: the search box lied in three different ways, and none of them is
  // visible from the source -- the tokenizer, the matcher and the render have to
  // agree, so this drives the real functions against a real game.
  motion.queryGrammar = await page.evaluate(async () => {
    const util = await import('/static/util.js');
    const game = {
      id: 1, game_id: 'g-1', name: 'The Beatles: Rock Band', sort_title: 'beatles rock band',
      alternate_names: ['Beatles RB'], platform: 'PC', genre: 'Music', developer: 'Harmonix',
      publisher: 'MTV', series: '', region: 'EU', notes: 'rhythm', source: 'steam',
      play_mode: 'keyboard', status: 'owned', progress: 'Beaten', rating: 5,
      favorite: true, installed: true, hidden: false, broken: false, portable: true,
      controller_support: 'xbox', tags: ['music'],
    };
    const t = q => util.parseQueryTokens(q);
    const m = (q, g) => util.advancedQueryMatches(g || game, q);
    return {
      // F7: a lone '-' used to produce an empty value, and every string
      // contains the empty string, so the negative matched nothing at all.
      loneMinusTokens: t('-'),
      loneMinusMatchesEverything: m('-'),
      // F11: the leading '-' could not bind to a quoted phrase, so
      // -"the beatles" tokenized as `-the` AND `beatles"` and returned the very
      // game it was meant to exclude.
      quotedNegativeTokens: t('-"the beatles"'),
      quotedNegativeRejectsGame: m('-"the beatles"'),
      quotedPositiveTokens: t('"rock band"'),
      quotedPositiveMatchesGame: m('"rock band"'),
      keyQuotedTokens: t('genre:"music"'),
      keyQuotedMatchesGame: m('genre:"music"'),
      // F12: an unknown key fell back to all 16 fields and returned a
      // plausible-looking result instead of showing the typo.
      unknownKeyTokens: t('platfrom:PC'),
      unknownKeyMatchesGame: m('platfrom:PC'),
      knownKeyMatchesGame: m('platform:PC'),
      allKeyStillWorks: m('all:harmonix'),
      plainWordTokens: t('beatles'),
      plainWordMatchesGame: m('beatles'),
    };
  });
  console.log('motion query grammar:', JSON.stringify(motion.queryGrammar, null, 2));

  // F7: a lone '-' used to be parsed as a *negation* with an empty value, and
  // every string contains the empty string, so it excluded every game and the
  // library rendered "0 games" with no explanation. The defect is the negation,
  // not the absence of results: a lone '-' is now an ordinary search for a
  // hyphen, so the token is non-negative and matches nothing for an honest
  // reason.
  (motion.queryGrammar.loneMinusTokens.some(token => token.negative)) && failures.push("motion.queryGrammar: a lone '-' is still parsed as a negation, so it excludes every game and the library shows 0 results (F7)");
  (motion.queryGrammar.loneMinusTokens.length !== 1 || motion.queryGrammar.loneMinusTokens[0].value !== '-') && failures.push(`motion.queryGrammar: a lone '-' should be one literal title token, got ${JSON.stringify(motion.queryGrammar.loneMinusTokens)} (F7)`);
  (motion.queryGrammar.quotedNegativeTokens.length !== 1) && failures.push(`motion.queryGrammar: -\"the beatles\" tokenized into ${motion.queryGrammar.quotedNegativeTokens.length} tokens instead of one (F11)`);
  (motion.queryGrammar.quotedNegativeRejectsGame) && failures.push("motion.queryGrammar: -\"the beatles\" matches a game named 'The Beatles', so the negation and the phrase are separate tokens (F11)");
  (!motion.queryGrammar.quotedPositiveMatchesGame) && failures.push("motion.queryGrammar: \"rock band\" no longer matches its own game's alternate name (F11)");
  (motion.queryGrammar.keyQuotedTokens.length !== 1) && failures.push("motion.queryGrammar: genre:\"music\" did not tokenize as one key/value token");
  (!motion.queryGrammar.keyQuotedMatchesGame) && failures.push("motion.queryGrammar: genre:\"music\" no longer matches genre 'Music'");
  (motion.queryGrammar.unknownKeyMatchesGame) && failures.push("motion.queryGrammar: an unrecognised key searched every field, so a typo returns plausible-looking wrong results (F12)");
  (!motion.queryGrammar.knownKeyMatchesGame) && failures.push("motion.queryGrammar: platform:PC no longer matches, so the F12 fix broke the real keys");
  (!motion.queryGrammar.allKeyStillWorks) && failures.push("motion.queryGrammar: the explicit all: key stopped working when the implicit fallback was removed (F12)");
  (motion.queryGrammar.plainWordTokens.length !== 1) && failures.push("motion.queryGrammar: a bare word did not tokenize as one title token");
  (!motion.queryGrammar.plainWordMatchesGame) && failures.push("motion.queryGrammar: a bare word no longer matches the title");

  // F3: a window resize used to reset the row height, and "row height unknown"
  // meant "render every row" -- 20,000 list rows in one innerHTML write.
  motion.listResizeBudget = await page.evaluate(async () => {
    const state = await import('/static/state.js');
    const library = await import('/static/library.js');
    const savedGames = state.AppState.games;
    const savedView = state.AppState.appSettings.library_view;
    const savedSearch = document.getElementById('sidebarSearch').value;
    // The 2-game demo library is long gone by this point in the run, and a
    // 2-row grid cannot violate a 20,000-row budget.
    const template = savedGames[0] || {name: 'Game', platform: 'PC', genre: 'Action'};
    state.AppState.games = Array.from({length: 20000}, (_, i) => ({
      ...template,
      id: 100000 + i,
      game_id: `g-perf-${i}`,
      name: `Perf Game ${i}`,
      sort_title: `perf game ${i}`,
    }));
    state.AppState._refreshCounter = (state.AppState._refreshCounter || 0) + 1;
    state.warmSearchIndex();
    state.AppState.appSettings.library_view = 'list';
    document.getElementById('sidebarSearch').value = '';
    document.getElementById('sidebarSearch').dispatchEvent(new Event('input', {bubbles: true}));
    state.invalidateFilterCache();
    library.renderGrid();
    await new Promise(r => setTimeout(r, 600));
    const total = state.filteredGames().length;
    const steady = document.querySelectorAll('#grid .list-row').length;
    window.dispatchEvent(new Event('resize'));
    await new Promise(r => setTimeout(r, 600));
    const afterResize = document.querySelectorAll('#grid .list-row').length;
    // And the zero-results path, which also zeroed the row height.
    document.getElementById('sidebarSearch').value = 'zzzz-no-such-game-zzzz';
    document.getElementById('sidebarSearch').dispatchEvent(new Event('input', {bubbles: true}));
    await new Promise(r => setTimeout(r, 500));
    document.getElementById('emptyClearFilters')?.click();
    await new Promise(r => setTimeout(r, 800));
    const afterClear = document.querySelectorAll('#grid .list-row').length;
    state.AppState.games = savedGames;
    state.AppState._refreshCounter = (state.AppState._refreshCounter || 0) + 1;
    state.AppState.appSettings.library_view = savedView;
    document.getElementById('sidebarSearch').value = savedSearch;
    document.getElementById('sidebarSearch').dispatchEvent(new Event('input', {bubbles: true}));
    state.invalidateFilterCache();
    return {total, steady, afterResize, afterClear};
  });
  console.log('motion list resize budget:', JSON.stringify(motion.listResizeBudget));
  (motion.listResizeBudget.total < 1000) && failures.push(`motion.listResizeBudget: only ${motion.listResizeBudget.total} games in the fixture, so the budget case is vacuous`);
  (motion.listResizeBudget.afterResize > 300) && failures.push(`motion.listResizeBudget: a resize rendered ${motion.listResizeBudget.afterResize} list rows; the 20k budget requires a virtual window (F3)`);
  (motion.listResizeBudget.afterClear > 300) && failures.push(`motion.listResizeBudget: clearing a 0-result search rendered ${motion.listResizeBudget.afterClear} list rows (F3)`);
  (motion.listResizeBudget.afterResize < 1) && failures.push("motion.listResizeBudget: the virtual window rendered nothing after a resize; the library blanked instead");

  console.log('motion inert released:', JSON.stringify(motion.inertReleased));
  (!motion.inertReleased.focusBefore) && failures.push("motion.inertReleased: the focus probe never worked, so this case cannot measure anything");
  (motion.inertReleased.clean.length) && failures.push(`motion.inertReleased: ${motion.inertReleased.clean.join(',')} was already inert before Big Box opened`);
  (motion.inertReleased.opened && !motion.inertReleased.whileOpen.length) && failures.push("motion.inertReleased: opening Big Box inerted nothing, so Tab still walks into the page behind the overlay");
  (motion.inertReleased.afterHide.length) && failures.push(`motion.inertReleased: hiding Big Box left ${motion.inertReleased.afterHide.join(',')} inert; the page can never be focused again`);
  (motion.inertReleased.focusBefore && !motion.inertReleased.focusWorks) && failures.push("motion.inertReleased: focus cannot move to a sidebar control after Big Box hides");

  (perfChecks.coverflowNodes > 11) && failures.push("perfChecks.coverflowNodes <= 11");
  (!perfChecks.gridNodeCount) && failures.push("perfChecks.gridNodeCount");
  (perfChecks.gridNodeCount >= perfChecks.visibleCount) && failures.push("perfChecks.gridNodeCount < perfChecks.visibleCount");
  (perfChecks.gridNodeCount > 300) && failures.push("perfChecks.gridNodeCount <= 300");
  (!perfChecks.delegatedClickOk) && failures.push("perfChecks.delegatedClickOk");
  (!perfChecks.filtered.includes('Chrono Library Game 19999')) && failures.push("perfChecks.filtered.includes('Chrono Library Game 19999')");
  (!perfChecks.searchIndexStats || perfChecks.searchIndexStats.games !== 20000) && failures.push("search index covers all 20000 games");
  (perfChecks.searchMs > 20) && failures.push("perfChecks.searchMs <= 20");

  (motion.gridEntrance.cards < 1) && failures.push("motion.gridEntrance.cards");
  (motion.gridEntrance.replayed > 0) && failures.push("motion: a search keystroke must not replay the grid entrance");
  (!motion.dialogExit.during) && failures.push("motion.dialogExit.during (exit animation plays)");
  (!motion.dialogExit.after) && failures.push("motion.dialogExit.after (dialog finishes closing)");
  (!motion.toast.kept) && failures.push("motion.toast: a notify() must not clobber a live Undo toast");
  (!motion.toast.topLayer) && failures.push("motion.toast: the toast container must live in the top layer");
  (!motion.toast.stacked) && failures.push("motion.toast: three messages must produce three toasts (a real stack, not a one-deep pending slot)");
  (!motion.toast.survived) && failures.push("motion.toast: hovering an Undo toast must pause its timer (WCAG 2.2.1)");
  (!motion.toastAboveDialog.raised) && failures.push("motion.toastAboveDialog: opening a dialog must not bury a live Undo toast under ::backdrop");
  // F2: the preview gate, measured rather than asserted from source.
  (!motion.restorePreview.armed.listed) && failures.push("motion.restorePreview: no backup was listed, so the case is vacuous");
  (motion.restorePreview.calls.diffs < 3) && failures.push(`motion.restorePreview: expected 3 diff fetches (success, failure, retry), got ${motion.restorePreview.calls.diffs}`);
  (motion.restorePreview.before.confirm) && failures.push("motion.restorePreview: the confirm button exists before any diff has run");
  (!motion.restorePreview.before.hidden) && failures.push("motion.restorePreview: the preview panel is visible before any diff has run");
  (!motion.restorePreview.armed.confirm) && failures.push("motion.restorePreview: a successful diff did not arm the restore");
  (motion.restorePreview.armed.retry) && failures.push("motion.restorePreview: a successful diff also offered a retry");
  (motion.restorePreview.armed.names.length !== 4) && failures.push(`motion.restorePreview: the preview listed ${motion.restorePreview.armed.names.length} games, expected all 4`);
  (motion.restorePreview.armed.names.includes('Added Since Backup') === false) && failures.push("motion.restorePreview: a game a restore would delete is not listed by name");
  (motion.restorePreview.armed.names.includes('Deleted Since Backup') === false) && failures.push("motion.restorePreview: a game a restore would bring back is not listed by name");
  (motion.restorePreview.armed.fields.length !== 1) && failures.push("motion.restorePreview: the per-field from/to detail is missing");
  (motion.restorePreview.armed.fields[0] && !motion.restorePreview.armed.fields[0].includes('Beaten')) && failures.push("motion.restorePreview: the field detail does not show the value the restore would write");
  (/save paths/i.test(motion.restorePreview.armed.text) === false) && failures.push("motion.restorePreview: the preview does not warn that a restore moves settings");
  (motion.restorePreview.failed.confirm) && failures.push("motion.restorePreview: a diff that 500s still armed the restore");
  (!motion.restorePreview.failed.retry) && failures.push("motion.restorePreview: a failed diff offers no way back");
  (!motion.restorePreview.failed.text.includes('archive unreadable')) && failures.push("motion.restorePreview: a failed diff swallows the server's reason");
  (motion.restorePreview.calls.restores !== 0) && failures.push("motion.restorePreview: previewing a restore must never POST one");
  (!motion.restorePreview.recovered.confirm) && failures.push("motion.restorePreview: retrying a failed diff does not re-arm the restore");
  // F1. ADR 0064 rule 3: the list must be non-empty or the case proves nothing.
  (motion.launchAudit.live.status !== 200) && failures.push(`motion.launchAudit: GET /api/v2/launch/audit returned ${JSON.stringify(motion.launchAudit.live)}`);
  (motion.launchAudit.live.scanned !== false) && failures.push("motion.launchAudit: a GET on a fresh library reported a scan; GET must never scan");
  (motion.launchAudit.rows !== 2) && failures.push(`motion.launchAudit: expected 2 cause rows, got ${motion.launchAudit.rows}`);
  (!motion.launchAudit.heads.includes('FLATPAK_NOT_INSTALLED · org.libretro.RetroArch')) && failures.push("motion.launchAudit: a cause row does not name what it is about");
  (motion.launchAudit.installButtons !== 1) && failures.push(`motion.launchAudit: expected one install fix (one per cause), got ${motion.launchAudit.installButtons}`);
  (!/Blocked: 20/.test(motion.launchAudit.totals)) && failures.push(`motion.launchAudit: totals line wrong: ${motion.launchAudit.totals}`);
  (motion.launchAudit.members !== 20) && failures.push(`motion.launchAudit: expanding a cause listed ${motion.launchAudit.members} games, expected 20`);
  (motion.launchAudit.pages !== 1) && failures.push(`motion.launchAudit: expected one member page request, got ${motion.launchAudit.pages}`);
  (motion.launchAudit.scansAfterRun !== 1) && failures.push("motion.launchAudit: the run button did not queue exactly one scan");
  (motion.launchAudit.hadTarget && !motion.launchAudit.selectedMatches) && failures.push("motion.launchAudit: Show me did not select the game");
  (motion.launchAudit.hadTarget && !motion.launchAudit.dialogClosed) && failures.push("motion.launchAudit: Show me left the dialog open");
  // G2: the three cases 1.15.0 planned and never wrote.
  // The 1.15 claim was "CLS = 0"; measured, steady-state interaction is 0.057
  // on the 20k fixture, all of it from the grid collapsing when a search
  // returns nothing. Asserting literally 0 would be a false claim in the other
  // direction, so the gate is the web-vitals "good" threshold and the measured
  // value is printed, with the toast and dialog phases required to contribute
  // nothing -- those are the surfaces this release changed.
  (!(motion.cls.total < 0.1)) && failures.push(`motion.cls: cumulative layout shift must stay under 0.1, got ${motion.cls.total}`);
  ((motion.cls.phases.dialog || 0) > 0.001) && failures.push(`motion.cls: opening a dialog shifted the page (${motion.cls.phases.dialog})`);
  ((motion.cls.phases.dialogClose || 0) > 0.001) && failures.push(`motion.cls: closing a dialog shifted the page (${motion.cls.phases.dialogClose})`);
  ((motion.cls.phases.toast || 0) > 0.001) && failures.push(`motion.cls: showing a toast shifted the page (${motion.cls.phases.toast})`);
  (!perfChecks.listPosition) && failures.push("perfChecks.listPosition: no positioned cards on the 20000-game fixture");
  (perfChecks.listPosition && !perfChecks.listPosition.ok) && failures.push("perfChecks.listPosition: aria-setsize/aria-posinset must cover the whole 20000-game result, not the rendered slice");
  (!motion.themePaint.hasHref) && failures.push("motion.themePaint: #themeStylesheet has no href on first paint");
  (!motion.themePaint.applied) && failures.push("motion.themePaint: the theme stylesheet did not load");
  (!motion.themePaint.switched) && failures.push("motion.themePaint: a theme switch must cross-fade through .theme-switching");
  (!motion.themePaint.settled) && failures.push("motion.themePaint: .theme-switching was never removed");
  (!motion.reduced.tokenZero) && failures.push("motion.reduced.tokenZero");
  (!motion.reduced.closedSync) && failures.push("motion.reduced.closedSync");
  (motion.reduced.animations > 0) && failures.push("motion.reduced.animations (nothing may animate)");
  console.log('JS errors:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
  (errors.length) && failures.push("no page or console errors");
  (!before.cardCount || !clicked) && failures.push("before.cardCount && clicked");
  (themeResults.some(r => !r.ok)) && failures.push("every stock theme renders its menus above the content");
  (!platformAfter || !platformAfter.ok) && failures.push("platformAfter && platformAfter.ok");
  (!igdbPlatformParam) && failures.push("igdbPlatformParam");
  (!v1MatchMapped) && failures.push("v1MatchMapped");
  (!matchReview.exported) && failures.push("matchReview.exported");
  (!matchReview.rowKeys) && failures.push("matchReview.rowKeys");
  (!matchReview.scoreComponents) && failures.push("matchReview.scoreComponents");
  (!matchReview.scoreReasons) && failures.push("matchReview.scoreReasons");
  (!matchReview.noBareConfidence) && failures.push("matchReview.noBareConfidence");
  (!matchReview.paginationCursor) && failures.push("matchReview.paginationCursor");
  (!matchReview.nextCursorUsed) && failures.push("matchReview.nextCursorUsed");
  (!matchReview.bulkLikelyNoPossible) && failures.push("matchReview.bulkLikelyNoPossible");
  (!matchReview.acceptUsesDecisions) && failures.push("matchReview.acceptUsesDecisions");
  (!matchReview.neverPosted) && failures.push("matchReview.neverPosted");
  (!matchReview.reopenWorks) && failures.push("matchReview.reopenWorks");
  (!matchReview.applyPayload || matchReview.applyPayload.replace_existing !== true) && failures.push("match review apply payload sets replace_existing");
  (!Array.isArray(matchReview.applyPayload.field_allow_list) || !matchReview.applyPayload.field_allow_list.includes('title')) && failures.push("Array.isArray(matchReview.applyPayload.field_allow_list) && matchReview.applyPayload.field_allow_list.includes('title')");
  (!Array.isArray(matchReview.applyPayload.media_allow_list) || !matchReview.applyPayload.media_allow_list.includes('cover')) && failures.push("Array.isArray(matchReview.applyPayload.media_allow_list) && matchReview.applyPayload.media_allow_list.includes('cover')");
  (!matchReview.matchReviewCssVarsOnly) && failures.push("matchReview.matchReviewCssVarsOnly");
  (!activityUi.exportedOpenActivity) && failures.push("activityUi.exportedOpenActivity");
  (!activityUi.exportedPartition) && failures.push("activityUi.exportedPartition");
  (!activityUi.partitionActive.includes('job-active')) && failures.push("activityUi.partitionActive.includes('job-active')");
  (!activityUi.partitionAttention.includes('job-attention')) && failures.push("activityUi.partitionAttention.includes('job-attention')");
  (!activityUi.partitionRecent.includes('job-recent')) && failures.push("activityUi.partitionRecent.includes('job-recent')");
  (activityUi.partitionRecent.includes('job-stale')) && failures.push("activity recent partition excludes stale jobs");
  (!activityUi.drawerOpen) && failures.push("activityUi.drawerOpen");
  (!activityUi.rowKeys) && failures.push("activityUi.rowKeys");
  (!activityUi.partialNotSuccess) && failures.push("activityUi.partialNotSuccess");
  (!activityUi.cancelDisabled) && failures.push("activityUi.cancelDisabled");
  (!activityUi.cancelNotCancellablePosted) && failures.push("activityUi.cancelNotCancellablePosted");
  (!activityUi.retryBodyJobIdOnly) && failures.push("activityUi.retryBodyJobIdOnly");
  (!activityUi.itemsKeys) && failures.push("activityUi.itemsKeys");
  (!activityUi.itemsFetch) && failures.push("activityUi.itemsFetch");
  (!activityUi.appImportsActivity) && failures.push("activityUi.appImportsActivity");
  (!activityUi.activityButtonOpens) && failures.push("activityUi.activityButtonOpens");
  (!activityUi.snapshotBeforeSse) && failures.push("activityUi.snapshotBeforeSse");
  (!activityUi.activityCssVarsOnly) && failures.push("activityUi.activityCssVarsOnly");
  (!setupUi.exported) && failures.push("setupUi.exported");
  (!setupUi.stepperPresent) && failures.push("setupUi.stepperPresent");
  (!setupUi.summaryKeys) && failures.push("setupUi.summaryKeys");
  (!setupUi.nextActionOne) && failures.push("setupUi.nextActionOne");
  (!setupUi.faugusVisible) && failures.push("setupUi.faugusVisible");
  (!setupUi.xbox360Visible) && failures.push("setupUi.xbox360Visible");
  (!setupUi.noFileInput) && failures.push("setupUi.noFileInput");
  (!setupUi.importsHasSetup) && failures.push("setupUi.importsHasSetup");
  (!setupUi.importsNoPrompt) && failures.push("setupUi.importsNoPrompt");
  (!setupUi.previewGetAfterPost) && failures.push("setupUi.previewGetAfterPost");
  (!setupUi.previewRowKeys) && failures.push("setupUi.previewRowKeys");
  (!setupUi.preflightCandidate) && failures.push("setupUi.preflightCandidate");
  (!setupUi.installFlatpakBody) && failures.push("setupUi.installFlatpakBody");
  (!setupUi.revalidateWaited) && failures.push("setupUi.revalidateWaited");
  (!setupUi.commitEmulatorChoices) && failures.push("setupUi.commitEmulatorChoices");
  (!setupUi.decisionsIncludeLaunch) && failures.push("setupUi.decisionsIncludeLaunch");
  (!setupUi.welcomeCompleted) && failures.push("setupUi.welcomeCompleted");
  (!setupUi.setupCssVarsOnly) && failures.push("setupUi.setupCssVarsOnly");
  (!setupBehavior.sourceIconsMonogram) && failures.push("setupBehavior.sourceIconsMonogram");
  (!setupBehavior.selectedSourceCard) && failures.push("setupBehavior.selectedSourceCard");
  (!setupBehavior.duplicateSourceBlocked) && failures.push("setupBehavior.duplicateSourceBlocked");
  (!setupBehavior.removeSelectedSource) && failures.push("setupBehavior.removeSelectedSource");
  (!setupBehavior.onlyCompletedStepsClickable) && failures.push("setupBehavior.onlyCompletedStepsClickable");
  (!setupBehavior.moreSourcesSurvivesRerender) && failures.push("setupBehavior.moreSourcesSurvivesRerender");
  (!setupBehavior.emptyScanMessage) && failures.push("setupBehavior.emptyScanMessage");
  (!setupBehavior.dismissedFlag) && failures.push("setupBehavior.dismissedFlag");
  (!setupBehavior.dismissNoReopen) && failures.push("setupBehavior.dismissNoReopen");
  (!setupBehavior.insightsHiddenOnEmpty) && failures.push("setupBehavior.insightsHiddenOnEmpty");
  (!setupBehavior.zeroImportHidesViewImported) && failures.push("setupBehavior.zeroImportHidesViewImported");
  (!setupBehavior.zeroImportMessage) && failures.push("setupBehavior.zeroImportMessage");
  (!setupBehavior.reopenRefreshesStep) && failures.push("setupBehavior.reopenRefreshesStep");
  (!setupBehavior.bigboxEmptyExplains) && failures.push("setupBehavior.bigboxEmptyExplains");
  (!setupBehavior.bigboxEmptyOpensWizard) && failures.push("setupBehavior.bigboxEmptyOpensWizard");
  (!setupBehavior.insightsCollapsePersists) && failures.push("setupBehavior.insightsCollapsePersists");
  (!setupBehavior.insightsToggleWritesStorage) && failures.push("setupBehavior.insightsToggleWritesStorage");
  (!nestedDialogOk.nestedClosed) && failures.push("nestedDialogOk.nestedClosed");
  (!nestedDialogOk.gameStillOpen) && failures.push("nestedDialogOk.gameStillOpen");
  (!nestedDialogOk.focusRestored) && failures.push("nestedDialogOk.focusRestored");
  (!dialogFocusOk.ok) && failures.push(`dialogFocusOk: ${dialogFocusOk.why}${dialogFocusOk.nested?.length ? ` (still open: ${dialogFocusOk.nested.join(',')})` : ''}`);
  (!readerCleanedOk) && failures.push("readerCleanedOk");
  (!gamepadLoopStopped) && failures.push("gamepadLoopStopped");
  (!gamepadPauseTrap.dismissControl) && failures.push("pause panel carries a dismiss control");
  (!gamepadPauseTrap.opened) && failures.push("gamepad pause button opens the pause overlay");
  (!gamepadPauseTrap.focusMoved) && failures.push("gamepad d-pad moves focus between pause buttons");
  (!gamepadPauseTrap.dismissed || !gamepadPauseTrap.bigBoxAlive) && failures.push("gamepad B dismisses only the pause overlay");
  (!bigboxCorrectness.activateExported) && failures.push("bigboxCorrectness.activateExported");
  (!bigboxCorrectness.appUsesActivate) && failures.push("bigboxCorrectness.appUsesActivate");
  (!bigboxCorrectness.preflightLaunch) && failures.push("bigboxCorrectness.preflightLaunch");
  (!bigboxCorrectness.noConfirmSessions) && failures.push("bigboxCorrectness.noConfirmSessions");
  (!bigboxCorrectness.noConfirmStorefront) && failures.push("bigboxCorrectness.noConfirmStorefront");
  (!bigboxCorrectness.allViewportCases) && failures.push("bigboxCorrectness.allViewportCases");
  (!bigboxCorrectness.shortcutWhileTyping) && failures.push("bigboxCorrectness.shortcutWhileTyping");
  (!queryEngine.cacheSameIdentity) && failures.push("queryEngine.cacheSameIdentity");
  (!queryEngine.longTokenFindsGame) && failures.push("queryEngine.longTokenFindsGame");
  (!queryEngine.unratedFacetKeepsUnrated) && failures.push("queryEngine.unratedFacetKeepsUnrated");
  (!queryEngine.importBatchIdFilter) && failures.push("queryEngine.importBatchIdFilter");
  (!queryEngine.typedImportBatchExact) && failures.push("queryEngine.typedImportBatchExact");
  (!queryEngine.resetClearsImportBatch) && failures.push("queryEngine.resetClearsImportBatch");
  (!queryEngine.notifySuccessLevel) && failures.push("queryEngine.notifySuccessLevel");
  (!queryEngine.notifyCompatInfo) && failures.push("queryEngine.notifyCompatInfo");
  (!queryEngine.notifySingleErrorLevel) && failures.push("queryEngine.notifySingleErrorLevel");
  (!queryEngine.stickyErrorVisible) && failures.push("queryEngine.stickyErrorVisible");
  (!queryEngine.errorBannerCopyWorks) && failures.push("queryEngine.errorBannerCopyWorks");
  (!appShell.setupCenterPresent) && failures.push("appShell.setupCenterPresent");
  (!appShell.welcomeWizardGone) && failures.push("appShell.welcomeWizardGone");
  (!appShell.welcomeShimHidden) && failures.push("appShell.welcomeShimHidden");
  (!appShell.setupButtonOpensCenter) && failures.push("appShell.setupButtonOpensCenter");
  (!appShell.welcomeShimOpensCenter) && failures.push("appShell.welcomeShimOpensCenter");
  (!appShell.reopenOpensCenter) && failures.push("appShell.reopenOpensCenter");
  (!appShell.queueStillQueue) && failures.push("appShell.queueStillQueue");
  (!appShell.activityDistinct) && failures.push("appShell.activityDistinct");
  (!appShell.pathBrowseHosts) && failures.push("appShell.pathBrowseHosts");
  (!appShell.noWelcomeImport) && failures.push("appShell.noWelcomeImport");
  (!appShell.noCompleteWelcome) && failures.push("appShell.noCompleteWelcome");
  (!appShell.noSetupImport) && failures.push("appShell.noSetupImport");
  (!appShell.importsHasSetup) && failures.push("appShell.importsHasSetup");
  (!appShell.importsNoPrompt) && failures.push("appShell.importsNoPrompt");
  (!appShell.hasActivityImport) && failures.push("appShell.hasActivityImport");
  (!appShell.noBindContextMenuA11y) && failures.push("appShell.noBindContextMenuA11y");
  (!appShell.noContextMenuListener) && failures.push("appShell.noContextMenuListener");
  (!appShell.noPromptInAppJs) && failures.push("appShell.noPromptInAppJs");
  (!appShell.noConfirmInAppJs) && failures.push("appShell.noConfirmInAppJs");
  (!appShell.nativePickFolderNoPrompt) && failures.push("appShell.nativePickFolderNoPrompt");
  (!a11yDialogs.noWindowPromptInDialogs) && failures.push("a11yDialogs.noWindowPromptInDialogs");
  (!a11yDialogs.shiftF10OpensMenu) && failures.push("a11yDialogs.shiftF10OpensMenu");
  (!a11yDialogs.escapeRestoresFocus) && failures.push("a11yDialogs.escapeRestoresFocus");
  (!a11yDialogs.confirmHasNamedTarget) && failures.push("a11yDialogs.confirmHasNamedTarget");
  (!a11yDialogs.pathBrowseFilled) && failures.push("a11yDialogs.pathBrowseFilled");
  (!a11yDialogs.coverBrowseFilled) && failures.push("a11yDialogs.coverBrowseFilled");
  (!a11yDialogs.settingsBrowseFilled) && failures.push("a11yDialogs.settingsBrowseFilled");
  (!libraryWorkspace.noMaybeShowWelcome) && failures.push("libraryWorkspace.noMaybeShowWelcome");
  (!libraryWorkspace.noWelcomeDialog) && failures.push("libraryWorkspace.noWelcomeDialog");
  (!libraryWorkspace.presetClearedOnPlatform) && failures.push("libraryWorkspace.presetClearedOnPlatform");
  (!libraryWorkspace.clearAllResets) && failures.push("libraryWorkspace.clearAllResets");
  (!libraryWorkspace.batchChipVisible) && failures.push("libraryWorkspace.batchChipVisible");
  (!libraryWorkspace.batchChipClears) && failures.push("libraryWorkspace.batchChipClears");
  (!libraryWorkspace.emptySetupBtn) && failures.push("libraryWorkspace.emptySetupBtn");
  (!libraryWorkspace.emptyOpensSetup) && failures.push("libraryWorkspace.emptyOpensSetup");
  (!libraryWorkspace.firstRunOpensSetup) && failures.push("libraryWorkspace.firstRunOpensSetup");
  (!libraryWorkspace.manualTileNoImg) && failures.push("libraryWorkspace.manualTileNoImg");
  (!libraryWorkspace.manualUsesReader) && failures.push("libraryWorkspace.manualUsesReader");
  (!libraryWorkspace.manualNoNativeExternal) && failures.push("libraryWorkspace.manualNoNativeExternal");
  (!libraryWorkspace.detailsResizeNoLibraryPost) && failures.push("libraryWorkspace.detailsResizeNoLibraryPost");
  (!masterySmoke.open) && failures.push("masterySmoke.open");
  (!masterySmoke.rendered) && failures.push("masterySmoke.rendered");
  (!partySmoke.bigBox) && failures.push("partySmoke.bigBox");
  (!partySmoke.overlayOpen) && failures.push("partySmoke.overlayOpen");
  (!partySmoke.setupOk) && failures.push("partySmoke.setupOk");
  (partySmoke.playersShown !== '3') && failures.push("partySmoke.playersShown === '3'");
  (!partySmoke.wheel) && failures.push("partySmoke.wheel");
  (!(partySmoke.upNext >= 1)) && failures.push("the party wheel lists at least one up-next game");
  (!partySmoke.closedByKeyboard) && failures.push("partySmoke.closedByKeyboard");
  (!partySmoke.bigBoxStillOpen) && failures.push("partySmoke.bigBoxStillOpen");
  (!constellationSmoke.open) && failures.push("constellationSmoke.open");
  (!constellationSmoke.rendered) && failures.push("constellationSmoke.rendered");
  (constellationSmoke.labels[0] !== 'Series') && failures.push("constellationSmoke.labels[0] === 'Series'");
  (!constellationSmoke.closed) && failures.push("constellationSmoke.closed");
  (!dropHonesty.pickerOpened) && failures.push("dropHonesty.pickerOpened");
  (!dropHonesty.libraryUnchanged) && failures.push("dropHonesty.libraryUnchanged");
  (!dropHonesty.noRomBinFolder) && failures.push("dropHonesty.noRomBinFolder");
  (dropHonesty.noImportToast === false) && failures.push("dropHonesty.noImportToast !== false");
  // F8, and a false claim this suite was shipping: `dropEmptyHonesty` asserted
  // that dropping a transfer with *no files* over #dropZone must open the "enter
  // the absolute path" prompt. That is the bug, not the contract -- a text drag
  // released over the drop zone (drag-selecting a game title, say) fires the same
  // event and used to summon an import prompt out of nowhere. The drop handler
  // now checks dataTransfer, so the honest expectation is the opposite: no
  // files, no prompt, no import.
  (dropEmptyHonesty.pickerOpened) && failures.push("dropEmptyHonesty.pickerOpened: a drop with no files must not open the import prompt (F8)");
  (dropEmptyHonesty.dropHasFiles) && failures.push("dropEmptyHonesty: the synthetic drop carried files, so the case does not test an empty transfer (F8)");
  (!dropEmptyHonesty.libraryUnchanged) && failures.push("dropEmptyHonesty.libraryUnchanged");
  (!dropEmptyHonesty.noImport) && failures.push("dropEmptyHonesty.noImport");
  const expectedGroups = {
    library: ['metadataButton','mediaButton','healthButton','constellationButton','masteryButton','bulkButton','tagsButton','playlistsButton','backupButton','historyButton','timeMachineButton','achievementsButton','saveFilterButton','savePresetButton'],
    sources: ['storefrontButton','emulatorsButton','steamButton','heroicButton','lutrisButton','arcadeButton','arcadeRoomButton','householdButton','discoveryButton'],
    personalize: ['themesButton','pluginsButton','settingsButton','whatsNewButton','fullscreenButton'],
    automation: ['webhooksButton','notificationsButton'],
  };
  for (const [key, ids] of Object.entries(expectedGroups)) {
    const got = appShell.toolGroups[key] || [];
    (JSON.stringify(got) !== JSON.stringify(ids)) && failures.push(`tool group ${key} lists ${ids.join(', ')}`);
  }
  (!hardening.longName.rendered || !hardening.longName.ellipsis || !hardening.longName.truncated) && failures.push("hardening.longName.rendered && hardening.longName.ellipsis && hardening.longName.truncated");
  (hardening.longName.pageOverflow) && failures.push("a long game name does not overflow the page");
  (!hardening.deleteSelected.ok || !hardening.deleteSelected.selectedCleared) && failures.push("hardening.deleteSelected.ok && hardening.deleteSelected.selectedCleared");
  (!hardening.deleteSelected.countDropped || hardening.deleteSelected.staleDetails) && failures.push("deleting the selection drops the count and clears stale details");
  (!hardening.filter.consistent || !hardening.filter.cardsRendered) && failures.push("hardening.filter.consistent && hardening.filter.cardsRendered");
  if (failures.length) {
    console.error(`SMOKE FAIL ${failures.length} check(s): ${failures.join('; ')}`);
    process.exit(1);
  }
  console.log('UI SMOKE PASSED');
})().catch(e => { console.error('SMOKE FAIL', e.message); console.error(e.stack); process.exit(1); });
