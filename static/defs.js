/* defs.js — Settings > Emulators: check, install and roll back the signed community definition pack (ADR 0061). */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { api, notify } from './state.js';

let bound = false;

function say(message) {
  const el = $('defsDetail');
  if (el) el.textContent = message;
}

async function refreshStatus() {
  const status = $('defsStatus');
  if (!status) return;
  try {
    const result = await api('/api/v2/emulators/defs/status');
    const installed = result.installed || [];
    status.textContent = installed.length
      ? t('defs.status_pack', { version: result.version || '?', count: installed.length })
      : t('defs.status_bundled');
    const edited = result.locally_modified || [];
    if (edited.length) status.textContent += ` ${t('defs.locally_modified', { names: edited.join(', ') })}`;
    $('defsRollback').hidden = !installed.length;
  } catch (error) {
    status.textContent = error.message || String(error);
  }
}

async function check() {
  say(t('defs.checking'));
  $('defsInstall').hidden = true;
  try {
    const result = await api('/api/v2/emulators/defs/update');
    if (!result.ok) {
      // No published pack is a normal state for a fresh channel, not an error.
      say(/404|not found/i.test(result.error || '') ? t('defs.none_published') : t('defs.check_failed', { error: result.error || '' }));
      return;
    }
    if (result.update_available) {
      say(t('defs.available', { version: result.version, notes: result.notes || '' }));
      $('defsInstall').hidden = false;
    } else {
      say(t('defs.up_to_date', { version: result.installed_version || result.version || '' }));
    }
  } catch (error) {
    say(t('defs.check_failed', { error: error.message || String(error) }));
  }
}

async function install() {
  say(t('defs.checking'));
  try {
    const result = await api('/api/v2/emulators/defs/update', { method: 'POST', body: '{}' });
    say(t('defs.installed', {
      version: result.version, count: (result.installed || []).length,
      updated: (result.updated || []).length, kept: (result.kept_local || []).length,
    }));
    $('defsInstall').hidden = true;
  } catch (error) {
    // A bad signature is a security event: say so plainly instead of a generic failure.
    say(error.message === 'signature_verification_failed' ? t('defs.rejected') : t('defs.check_failed', { error: error.message || String(error) }));
  }
  refreshStatus();
}

async function rollback() {
  try {
    const result = await api('/api/v2/emulators/defs/rollback', { method: 'POST', body: '{}' });
    say(t('defs.rolled_back', { count: (result.removed || []).length }));
    notify(t('defs.rolled_back', { count: (result.removed || []).length }));
  } catch (error) {
    say(escapeHtml(error.message || String(error)));
  }
  refreshStatus();
}

function initDefs() {
  if (!$('defsPanel')) return;
  if (!bound) {
    bound = true;
    $('defsCheck').onclick = check;
    $('defsInstall').onclick = install;
    $('defsRollback').onclick = rollback;
  }
  refreshStatus();
}

export { initDefs };
