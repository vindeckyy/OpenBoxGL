#!/bin/bash
# UI smoke test: boots web_app.py against a temp data dir, drives it with
# puppeteer, and asserts the AppState refactor renders a working grid.
set -u
cd "$(dirname "$0")/.."

DATA_DIR=$(mktemp -d /tmp/obx-ui-smoke.XXXXXX)
export OPENBOX_DATA_DIR="$DATA_DIR"

python3 -B web_app.py --no-browser > "$DATA_DIR/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; rm -rf "$DATA_DIR"' EXIT

# wait for the server token
for i in $(seq 1 30); do
  [ -f "$DATA_DIR/server.token" ] && [ -f "$DATA_DIR/server.port" ] && break
  sleep 0.5
done
TOKEN=$(cat "$DATA_DIR/server.token")
PORT=$(cat "$DATA_DIR/server.port")

# seed the library
python3 -B - <<'EOF'
import os
from openbox import save_state
save_state({"games": [
    {"name": "Quake", "platform": "PC", "genre": "FPS", "year": "1996", "developer": "id Software", "path": "/bin/true", "favorite": True, "rating": 5, "progress": "Beaten", "play_count": 12, "playtime_seconds": 5400},
    {"name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "year": "1995", "path": "/bin/true"},
], "profiles": {}, "history": [], "settings": {}, "playlists": []})
print("seeded")
EOF

TOKEN="$TOKEN" PORT="$PORT" node - <<'EOF'
const puppeteer = require('./scripts/node_modules/puppeteer');
(async () => {
  const browser = await puppeteer.launch({headless: 'new', args: ['--no-sandbox']});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await page.goto(`http://127.0.0.1:${process.env.PORT}?token=${process.env.TOKEN}`, {waitUntil: 'networkidle2', timeout: 20000});
  await new Promise(r => setTimeout(r, 3000));
  const before = await page.evaluate(() => ({
    gamesLen: AppState.games.length,
    cardCount: document.querySelectorAll('.card').length,
    filtered: filteredGames().map(g => g.name),
    status: document.getElementById('status').textContent,
  }));
  const clicked = await page.evaluate(() => { const c = document.querySelector('.card-main'); if (!c) return false; c.click(); return true; });
  await new Promise(r => setTimeout(r, 800));
  const after = await page.evaluate(() => ({ details: document.getElementById('details').innerText.slice(0, 200) }));
  console.log(JSON.stringify({before, clicked, after}, null, 2));
  const themeNames = ['Midnight Circuit', 'Harbor Light', 'Cinema Marquee', 'Nordic Mist', 'Phosphor Terminal'];
  const themeResults = [];
  for (const name of themeNames) {
    await page.evaluate(async (n, t) => {
      await fetch('/api/themes/select', {method: 'POST', headers: {'X-OpenBox-Token': t, 'Content-Type': 'application/json'}, body: JSON.stringify({name: n})});
    }, name, process.env.TOKEN);
    // The app scrubs ?token= from the URL after reading it, so re-apply the
    // token on each navigation (the server.token file is the canonical source).
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
  // Big Box hybrid platform switching: selecting a .bigbox-platform button
  // must update AppState.bigBoxPlatform (regression: the button carried a
  // broken data-bigbox-AppState.platform attribute that silently no-op'd).
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
  // IGDB search must pass the game platform through as ?platform= (regression:
  // the query string was &AppState.platform =..., so IGDB never filtered).
  // Intercept the request: assert its URL, and answer with a fake 200 so the
  // credential-less test server never sees it (it would otherwise log a 400
  // console error and trip the global errors check below).
  const igdbRequests = [];
  await page.setRequestInterception(true);
  page.on('request', request => {
    if (request.url().includes('/api/metadata/igdb/search')) {
      igdbRequests.push(request.url());
      request.respond({status: 200, contentType: 'application/json', body: '{"results":[]}'});
    } else {
      request.continue();
    }
  });
  const igdbChecked = await page.evaluate(async () => {
    const game = AppState.games.find(g => g.name === 'Quake');
    if (!game) return {ok: false, reason: 'Quake not seeded'};
    document.getElementById('databaseMetadataButton')?.click();
    if (!document.getElementById('metadataDialog').open) {
      // No details pane open (no game selected): open the dialog via the
      // module's exported opener, which selects the game first.
      const mod = await import('/static/metadata.js');
      mod.openMetadata(game);
    }
    if (document.getElementById('metadataQuery').value === '') document.getElementById('metadataQuery').value = game.name;
    document.getElementById('searchIgdb').click();
    return true;
  });
  await new Promise(r => setTimeout(r, 2000));
  const igdbResult = igdbRequests.length ? igdbRequests[0] : 'NO IGDB REQUEST';
  const igdbPlatformParam = igdbResult !== 'NO IGDB REQUEST' && /[?&]platform=/.test(igdbResult);
  const igdbPayload = await page.evaluate(() => ({open: document.getElementById('metadataDialog').open, query: document.getElementById('metadataQuery').value}));
  console.log('igdb search:', JSON.stringify({igdbChecked, igdbResult, igdbPlatformParam, igdbPayload}));
  console.log('JS errors:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
  if (errors.length) process.exit(1);
  if (!before.cardCount || !clicked) process.exit(1);
  if (themeResults.some(r => !r.ok)) process.exit(1);
  if (!platformAfter || !platformAfter.ok) process.exit(1);
  if (!igdbPlatformParam) process.exit(1);
})().catch(e => { console.error('SMOKE FAIL', e.message); process.exit(1); });
EOF
