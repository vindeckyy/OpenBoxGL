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
    await page.evaluate(async (n) => {
      const t = new URLSearchParams(location.search).get('token');
      await fetch('/api/themes/select', {method: 'POST', headers: {'X-OpenBox-Token': t, 'Content-Type': 'application/json'}, body: JSON.stringify({name: n})});
    }, name);
    await page.reload({waitUntil: 'domcontentloaded', timeout: 20000});
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
  console.log('JS errors:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
  if (errors.length) process.exit(1);
  if (!before.cardCount || !clicked) process.exit(1);
  if (themeResults.some(r => !r.ok)) process.exit(1);
})().catch(e => { console.error('SMOKE FAIL', e.message); process.exit(1); });
EOF
