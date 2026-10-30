/* Compatibility report in Settings > About (plan W6-02, W6-03).
 *
 * The report is read-only and comes from /api/v2/compat/report. Each section names its items
 * with a disposition code; this module turns the codes into text. A theme with missing tokens
 * still renders, and the list says which tokens fall back to the built-in value.
 */
import { $, escapeHtml } from './util.js';
import { api } from './state.js';
import { t } from './i18n.js';

const DISPOSITION_KEYS = {
  both: 'compat.disposition_both',
  '2.0-only': 'compat.disposition_2_only',
  refused: 'compat.disposition_refused',
  unreadable: 'compat.disposition_unreadable',
};

function item(html) {
  return `<li>${html}</li>`;
}

function section(titleKey, rows) {
  return `<h4>${escapeHtml(t(titleKey))}</h4><ul>${rows.join('')}</ul>`;
}

function themeRows(themes) {
  return themes.map(row => {
    const name = `<strong>${escapeHtml(row.name)}</strong>`;
    if (row.disposition === 'unreadable') return item(`${name}: ${escapeHtml(t('compat.disposition_unreadable'))}`);
    const note = row.missing.length
      ? t('compat.theme_fallbacks', { tokens: row.missing.join(', ') })
      : t('compat.theme_complete');
    return item(`${name}: ${escapeHtml(note)}`);
  });
}

function backupRows(backups) {
  return backups.map(row => {
    const label = DISPOSITION_KEYS[row.disposition] || 'compat.disposition_refused';
    return item(`<strong>${escapeHtml(row.name)}</strong>: ${escapeHtml(t(label))}`);
  });
}

export function renderCompatReport(report) {
  const library = report.library || {};
  const settings = report.settings || {};
  const apiInfo = report.api || {};
  const sync = report.sync || {};
  const libraryLine = t('compat.library_line', {
    schema: library.schema_version ?? t('compat.unknown'),
    mode: library.path_mode || 'absolute',
  });
  const upgrade = library.upgraded_from
    ? t('compat.upgraded_from', { from: library.upgraded_from })
    : t('compat.no_upgrade_backup');
  const unknown = settings.unknown_keys || [];
  const syncLine = !sync.configured
    ? t('compat.sync_none')
    : (sync.found ? t('compat.sync_found') : t('compat.sync_missing'));
  return [
    section('compat.section_library', [item(escapeHtml(libraryLine)), item(escapeHtml(upgrade))]),
    section('compat.section_themes', themeRows(report.themes || [])),
    section('compat.section_backups', backupRows(report.backups || [])),
    section('compat.section_settings', [item(escapeHtml(unknown.length
      ? t('compat.settings_unknown', { keys: unknown.join(', ') })
      : t('compat.settings_none')))]),
    section('compat.section_api', [
      item(escapeHtml(t('compat.api_public', {
        prefix: apiInfo.public_prefix || '/api/v2',
        host: apiInfo.host_channel || '/api/host',
      }))),
      item(escapeHtml(apiInfo.v1_bridge ? t('compat.api_v1_on') : t('compat.api_v1_off'))),
    ]),
    section('compat.section_sync', [item(escapeHtml(syncLine))]),
  ].join('');
}

export async function showCompatReport() {
  const out = $('compatReport');
  out.hidden = false;
  out.textContent = t('compat.checking');
  try {
    const report = await api('/api/v2/compat/report');
    out.innerHTML = renderCompatReport(report);
  } catch (error) {
    out.textContent = error.message;
  }
}

export function initCompatReport() {
  $('compatReportButton').onclick = showCompatReport;
}
