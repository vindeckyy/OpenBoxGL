const fs = require('fs');
const puppeteer = require('./node_modules/puppeteer');

// Flagship 5 smoke pass: boot with --bigbox (the harness navigates to the
// exact URL the flag produces: base + &deeplink=bigbox), assert Big Box is
// showing, then drive the on-screen keyboard and assert the grid filters.
// Records into `failures` instead of exiting on the first failed check.
const failures = [];

(async () => {
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined);
  const launchOpts = {headless: 'new', args: ['--no-sandbox']};
  if (executablePath) launchOpts.executablePath = executablePath;
  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  // Pin reduced motion for the functional flows: headless Chrome inherits the OS setting, so leaving it unset makes\r\n  // dialog closes synchronous on Windows and animated on Linux. The motion block below turns real motion on explicitly.
  await page.emulateMediaFeatures([{name: 'prefers-reduced-motion', value: 'reduce'}]);
  page.on('pageerror', e => failures.push('pageerror: ' + e.message));

  const deeplink = process.env.SMOKE_DEEPLINK ? `&deeplink=${process.env.SMOKE_DEEPLINK}` : '';
  await page.goto(`http://127.0.0.1:${process.env.PORT}/?token=${process.env.TOKEN}${deeplink}`, {waitUntil: 'networkidle2', timeout: 20000});
  await new Promise(r => setTimeout(r, 3000));

  // 1. The --bigbox deeplink lands in Big Box.
  const inBigBox = await page.evaluate(() => !document.getElementById('bigBox').hidden);
  if (!inBigBox) failures.push('boot --bigbox did not land in Big Box');

  // 2. Focusing the hybrid search opens the OSK; typing on it filters the grid.
  const oskOpened = await page.evaluate(() => {
    const search = document.getElementById('bigBoxHybridSearch');
    if (!search || search.hidden) return 'search input not visible';
    search.focus();
    return document.getElementById('bigBoxOsk').hidden ? 'osk did not open' : 'ok';
  });
  if (oskOpened !== 'ok') {
    failures.push('osk open: ' + oskOpened);
  } else {
    // The hybrid tab follows the game Big Box opened on (renderBigBox), so the
    // typed prefix is taken from that game: the check does not depend on which
    // platform tab the boot game sorts into.
    const boot = await page.evaluate(() => AppState.bigBoxGames[AppState.bigBoxIndex]?.name || '');
    const prefix = boot.slice(0, 2).toLowerCase();
    await page.evaluate(prefixKeys => {
      const key = label => [...document.querySelectorAll('#bigBoxOsk .osk-key')].find(b => b.textContent === label);
      for (const ch of prefixKeys) key(ch).click();
    }, prefix);
    await new Promise(r => setTimeout(r, 500));
    const filtered = await page.evaluate(() => ({
      query: document.getElementById('bigBoxHybridSearch').value,
      games: AppState.bigBoxGames.map(g => g.name),
    }));
    console.log('osk filter:', JSON.stringify(filtered));
    if (filtered.query !== prefix) failures.push(`osk typed query is ${JSON.stringify(filtered.query)}, expected ${JSON.stringify(prefix)}`);
    if (!filtered.games.includes(boot)) {
      failures.push(`osk filter left ${JSON.stringify(filtered.games)}, expected it to keep ${JSON.stringify(boot)}`);
    }
    // Backspace removes the last char and re-filters.
    await page.evaluate(() => {
      [...document.querySelectorAll('#bigBoxOsk .osk-key')].find(b => b.textContent === '⌫').click();
    });
    await new Promise(r => setTimeout(r, 500));
    const afterBackspace = await page.evaluate(() => ({
      query: document.getElementById('bigBoxHybridSearch').value,
      count: AppState.bigBoxGames.length,
    }));
    if (afterBackspace.query !== prefix.slice(0, 1) || afterBackspace.count < 1) {
      failures.push(`osk backspace left ${JSON.stringify(afterBackspace)}, expected query ${JSON.stringify(prefix.slice(0, 1))} with at least 1 game`);
    }
  }

  // 3. The bigbox_start_at_launch setting alone (no deeplink) boots into Big Box.
  await page.goto(`http://127.0.0.1:${process.env.PORT}/?token=${process.env.TOKEN}`, {waitUntil: 'networkidle2', timeout: 20000});
  await new Promise(r => setTimeout(r, 3000));
  const settingBooted = await page.evaluate(() => !document.getElementById('bigBox').hidden);
  if (!settingBooted) failures.push('bigbox_start_at_launch did not boot into Big Box');

  await browser.close();
  if (failures.length) {
    console.error(`BIGBOX SMOKE FAIL ${failures.length} check(s): ${failures.join('; ')}`);
    process.exit(1);
  }
  console.log('BIGBOX SMOKE PASSED');
})().catch(e => { console.error('BIGBOX SMOKE FAIL', e.message); process.exit(1); });
