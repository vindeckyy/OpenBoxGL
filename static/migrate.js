/* Pre-app migration page (plan W1-12, W6-01).
 *
 * When the library was upgraded to the v7 format and the user has not seen the report yet,
 * this gate covers the app: it shows what the migration did, where the copy of the old library
 * is, and offers the rollback. The library is upgraded on purpose, so the page reports the
 * change and hands the choice to the user.
 */
import { $, escapeHtml } from './util.js';
import { api } from './state.js';
import { t } from './i18n.js';

function reportLine(status) {
  const report = status.report || {};
  const counts = report.counts || {};
  const from = report.from_schema ?? '-';
  const to = report.to_schema ?? '-';
  const games = counts.games ?? report.games ?? '-';
  return { from, to, games };
}

function renderGate(status) {
  const facts = reportLine(status);
  $('migrationFrom').textContent = String(facts.from);
  $('migrationTo').textContent = String(facts.to);
  $('migrationGames').textContent = String(facts.games);
  $('migrationBackup').textContent = status.backup_path || t('migration.backup_missing');
  renderReadiness(status.readiness || []);
  $('migrationGate').hidden = false;
  document.body.classList.add('migration-pending');
}

// The readiness checks the 1.19.2 preflight would have shown. They run here
// instead, on 2.0's first launch, before the library is used.
function renderReadiness(items) {
  const list = $('migrationReadinessList');
  const section = $('migrationReadiness');
  if (!list || !section) return;
  list.innerHTML = items.map(item => `<li><strong>${escapeHtml(item.label || item.kind || '')}</strong> ${escapeHtml(item.detail || '')}</li>`).join('');
  section.hidden = items.length === 0;
}

async function backUpFirst() {
  const status = $('migrationStatus');
  const button = $('migrationBackupButton');
  if (button) button.disabled = true;
  status.textContent = t('migration.backup_running');
  try {
    const result = await api('/api/v2/setup/migration/backup', { method: 'POST', body: JSON.stringify({}) });
    status.textContent = t('migration.backup_done', { name: result.name || result.archive || '' });
  } catch (error) {
    status.textContent = t('migration.error', { message: error.message });
    if (button) button.disabled = false;
  }
}

async function openLibrary() {
  try {
    await api('/api/v2/setup/migration/acknowledge', { method: 'POST', body: JSON.stringify({}) });
  } catch (error) {
    $('migrationStatus').textContent = error.message;
    return;
  }
  $('migrationGate').hidden = true;
  document.body.classList.remove('migration-pending');
}

async function rollBack() {
  const status = $('migrationStatus');
  try {
    const preview = await api('/api/v2/setup/migration/rollback', { method: 'POST', body: JSON.stringify({}) });
    if (preview.status === 'confirmation_required') {
      const lost = (preview.lost || []).length;
      status.textContent = t('migration.rollback_pending', { count: lost });
      $('migrationRollback').disabled = false;
      $('migrationRollback').dataset.confirmed = '1';
      return;
    }
  } catch (error) {
    status.textContent = t('migration.error', { message: error.message });
    return;
  }
}

async function rollBackConfirmed() {
  const status = $('migrationStatus');
  try {
    await api('/api/v2/setup/migration/rollback', { method: 'POST', body: JSON.stringify({ confirmed: true }) });
  } catch (error) {
    status.textContent = t('migration.error', { message: error.message });
    return;
  }
  status.textContent = t('migration.rolled_back');
  $('migrationGate').hidden = false;
}

export function initMigrationGate() {
  $('migrationRollback').onclick = () => {
    if ($('migrationRollback').dataset.confirmed === '1') rollBackConfirmed();
    else rollBack();
  };
  $('migrationBackupButton').onclick = backUpFirst;
  $('migrationOpen').onclick = openLibrary;
  api('/api/v2/setup/migration/status')
    .then(status => {
      if (!status || !status.pending) return;
      renderGate(status);
    })
    .catch(() => {
      // A server that cannot answer the question is not a reason to block the app.
    });
}
