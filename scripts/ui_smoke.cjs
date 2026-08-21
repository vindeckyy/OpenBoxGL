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
      // frame-ancestors CSPenforced top-level is not a real error for the smoke harness
      if (text.includes('frame-ancestors') || text.includes("Framing 'http")) return;
      errors.push('console: ' + text);
    }
  });
  await page.goto(`http://127.0.0.1:${process.env.PORT}?token=${process.env.TOKEN}`, {waitUntil:'networkidle2', timeout:20000});
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
    await page.click('.topbar-tools summary');
    await new Promise(r => setTimeout(r, 300));
    const ok = await page.evaluate(() => {
      const menu = document.querySelector('.topbar-tools .tool-menu');
      if (!menu) return false;
      const rect = menu.getBoundingClientRect();
      const topEl = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return menu.contains(topEl);
    });
    themeResults.push({name, ok});
    await page.evaluate(() => document.querySelector('.topbar-tools')?.removeAttribute('open'));
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
  if (!dialogFocusOk) process.exit(1);
  if (!readerCleanedOk) process.exit(1);
  if (!gamepadLoopStopped) process.exit(1);
  console.log('UI SMOKE PASSED');
})().catch(e => { console.error('SMOKE FAIL', e.message); process.exit(1); });
