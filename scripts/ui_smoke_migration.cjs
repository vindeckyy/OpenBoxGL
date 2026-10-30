const fs = require('fs');
const puppeteer = require('./node_modules/puppeteer');

// Smoke for the pre-app migration gate (plan W1-12, W6-01).
//
// The server migrates a v6 library at startup. The gate must cover the app,
// name what the migration did and where the copy of the old file is, list the
// readiness items the 1.19.2 preflight would have shown, write a backup when
// the user asks for one, and hand the library over when it is opened.

const failures = [];

(async () => {
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined);
  const launchOpts = {headless: 'new', args: ['--no-sandbox']};
  if (executablePath) launchOpts.executablePath = executablePath;
  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await page.goto(`http://127.0.0.1:${process.env.PORT}?token=${process.env.TOKEN}`, {waitUntil: 'networkidle2', timeout: 20000});
  await new Promise(r => setTimeout(r, 2500));

  const gate = await page.evaluate(() => {
    const el = document.getElementById('migrationGate');
    return {
      visible: !!el && !el.hidden,
      pending: document.body.classList.contains('migration-pending'),
      from: document.getElementById('migrationFrom')?.textContent || '',
      to: document.getElementById('migrationTo')?.textContent || '',
      games: document.getElementById('migrationGames')?.textContent || '',
      backup: document.getElementById('migrationBackup')?.textContent || '',
      readinessVisible: !document.getElementById('migrationReadiness')?.hidden,
      readinessItems: document.querySelectorAll('#migrationReadinessList li').length,
      hasBackupButton: !!document.getElementById('migrationBackupButton'),
    };
  });
  if (!gate.visible) failures.push('the migration gate is not shown');
  if (!gate.pending) failures.push('the page is not marked as pending');
  if (gate.from !== '6') failures.push(`the gate does not name the old schema (${gate.from})`);
  if (gate.to !== '7') failures.push(`the gate does not name the new schema (${gate.to})`);
  if (!gate.backup.includes('library.v6.backup.json')) {
    failures.push(`the gate does not name the copy of the old library (${gate.backup})`);
  }
  if (!gate.hasBackupButton) failures.push('the gate has no backup button');
  if (!gate.readinessVisible || !gate.readinessItems) failures.push('the gate lists no readiness items');

  // "Back up first" writes an archive and says which one.
  await page.evaluate(() => document.getElementById('migrationBackupButton').click());
  await new Promise(r => setTimeout(r, 3000));
  const backupStatus = await page.evaluate(() => document.getElementById('migrationStatus')?.textContent || '');
  if (!/OpenBoxBackup-/.test(backupStatus)) {
    failures.push(`the backup button did not report an archive (${backupStatus})`);
  }

  // "Open the library" hands the library over and uncovers the app.
  await page.evaluate(() => document.getElementById('migrationOpen').click());
  await new Promise(r => setTimeout(r, 1500));
  const opened = await page.evaluate(() => ({
    hidden: document.getElementById('migrationGate')?.hidden,
    pending: document.body.classList.contains('migration-pending'),
  }));
  if (!opened.hidden) failures.push('the gate stayed open after the library was opened');
  if (opened.pending) failures.push('the page is still marked as pending');

  if (errors.length) failures.push(...errors);
  console.log('migration gate:', JSON.stringify({gate, backupStatus, opened}));
  await browser.close();
  if (failures.length) {
    console.error('MIGRATION SMOKE FAILED');
    failures.forEach(f => console.error(' - ' + f));
    process.exit(1);
  }
  console.log('MIGRATION SMOKE PASSED');
})().catch(e => { console.error('MIGRATION SMOKE ERROR', e.message); process.exit(1); });
