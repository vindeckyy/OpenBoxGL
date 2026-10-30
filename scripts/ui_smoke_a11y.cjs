const fs = require('fs');
const puppeteer = require('./node_modules/puppeteer');

// Accessibility smoke for the Settings surfaces added in 2.0 (plan W7-08).
//
// Checks the keyboard contract, not the mouse one: focus moves into the settings dialog,
// every new control is reachable with Tab in document order, every new control has an
// accessible name, Escape leaves the dialog (no keyboard trap), and the reduced-motion
// setting collapses the motion tokens.

const failures = [];

const PANELS = [
  { category: 'library', ids: ['libraryMoveFolder', 'libraryMoveBrowse', 'libraryIntegrityCheck', 'libraryMovePreview'] },
  { category: 'sessions', ids: ['backupFormat'] },
  { category: 'about', ids: ['compatReportButton'] },
];

(async () => {
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined);
  const launchOpts = {headless: 'new', args: ['--no-sandbox']};
  if (executablePath) launchOpts.executablePath = executablePath;
  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  await page.emulateMediaFeatures([{name: 'prefers-reduced-motion', value: 'reduce'}]);
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await page.goto(`http://127.0.0.1:${process.env.PORT}?token=${process.env.TOKEN}`, {waitUntil: 'networkidle2', timeout: 20000});
  await new Promise(r => setTimeout(r, 2500));

  // 1. Reduced motion collapses the motion tokens (static/app.css).
  const motion = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    return {
      durBase: (root.getPropertyValue('--dur-base') || '').trim(),
      durSlow: (root.getPropertyValue('--dur-slow') || '').trim(),
    };
  });
  if (!motion.durBase || parseFloat(motion.durBase) > 0.01) {
    failures.push(`reduced motion does not collapse --dur-base (${motion.durBase})`);
  }

  // 2. The settings dialog takes focus.
  await page.evaluate(() => document.getElementById('settingsButton').click());
  await new Promise(r => setTimeout(r, 900));
  const opened = await page.evaluate(() => {
    const dialog = document.getElementById('settingsDialog');
    return { open: !!dialog.open, focusedInside: dialog.contains(document.activeElement) };
  });
  if (!opened.open) failures.push('the settings dialog did not open');
  if (!opened.focusedInside) failures.push('focus did not move into the settings dialog');

  const visited = [];
  for (const panel of PANELS) {
    await page.evaluate((category) => {
      const button = document.querySelector(`#settingsNav button[data-settings-category="${category}"]`);
      if (button) button.click();
    }, panel.category);
    await new Promise(r => setTimeout(r, 400));
    const before = visited.length;
    for (let i = 0; i < 160; i += 1) {
      await page.keyboard.press('Tab');
      const id = await page.evaluate(() => document.activeElement?.id || '');
      if (id) visited.push(id);
      const found = panel.ids.every(wanted => visited.slice(before).includes(wanted));
      if (found) break;
    }
    const panelVisits = visited.slice(before);
    for (const wanted of panel.ids) {
      if (!panelVisits.includes(wanted)) failures.push(`Tab cannot reach #${wanted} in the ${panel.category} panel`);
    }
    // Compare the order the keyboard reached the controls in with their document order.
    const domOrder = await page.evaluate((ids) => {
      const all = Array.from(document.querySelectorAll('*'));
      return ids.slice().sort((a, b) => all.findIndex(el => el.id === a) - all.findIndex(el => el.id === b));
    }, panel.ids);
    const visitOrder = domOrder.filter(id => panelVisits.includes(id))
      .sort((a, b) => panelVisits.indexOf(a) - panelVisits.indexOf(b));
    if (domOrder.join(',') !== visitOrder.join(',')) {
      failures.push(`focus order in the ${panel.category} panel is not document order (${visitOrder.join(',')} vs ${domOrder.join(',')})`);
    }
  }

  // 3. Every new control has an accessible name.
  const names = await page.evaluate((ids) => {
    const accessibleName = (element) => {
      const aria = element.getAttribute('aria-label');
      if (aria && aria.trim()) return aria.trim();
      const labelledBy = element.getAttribute('aria-labelledby');
      if (labelledBy) return (document.getElementById(labelledBy)?.textContent || '').trim();
      const wrapping = element.closest('label');
      if (wrapping) return (wrapping.textContent || '').trim();
      const forLabel = element.id ? document.querySelector(`label[for="${element.id}"]`) : null;
      if (forLabel) return (forLabel.textContent || '').trim();
      return (element.textContent || '').trim();
    };
    return ids.map((id) => {
      const element = document.getElementById(id);
      return { id, name: element ? accessibleName(element) : '' };
    });
  }, PANELS.flatMap(panel => panel.ids));
  for (const row of names) {
    if (!row.name) failures.push(`#${row.id} has no accessible name`);
  }

  // 4. Escape leaves the dialog: no keyboard trap.
  await page.keyboard.press('Escape');
  await new Promise(r => setTimeout(r, 600));
  const closed = await page.evaluate(() => {
    const dialog = document.getElementById('settingsDialog');
    return { open: !!dialog.open, focusedInside: dialog.contains(document.activeElement) };
  });
  if (closed.open) failures.push('Escape did not close the settings dialog');
  if (closed.focusedInside) failures.push('focus stayed inside the closed dialog');

  if (errors.length) failures.push(...errors);
  console.log('a11y:', JSON.stringify({ motion, names, visited: visited.slice(0, 16) }));
  await browser.close();
  if (failures.length) {
    console.error('A11Y SMOKE FAILED');
    failures.forEach(failure => console.error('  ' + failure));
    process.exit(1);
  }
  console.log('A11Y SMOKE PASSED');
})().catch(e => { console.error('A11Y SMOKE ERROR', e.message); console.error(e.stack); process.exit(1); });
