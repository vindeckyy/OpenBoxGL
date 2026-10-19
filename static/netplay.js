import { $, escapeHtml } from './util.js';
import { api, notify } from './state.js';
import { openDialog } from './dialogs.js';
import { t } from './i18n.js';

function address(row) {
  return `${row.host}:${row.port}`;
}

async function openNetplay() {
  const dialog = $('netplayDialog');
  const target = $('netplayLobby');
  target.innerHTML = '<p class="description">Loading the RetroArch lobby…</p>';
  if (!dialog.open) openDialog(dialog);
  try {
    const result = await api('/api/v2/netplay/lobby');
    if (!result.available) {
      target.innerHTML = `<p class="description">${escapeHtml(result.error || 'Netplay lobby is unavailable.')}</p>`;
      return;
    }
    target.innerHTML = result.rows.length ? result.rows.map(row => `<div class="storefront-row"><div><strong>${escapeHtml(row.game || 'Netplay room')}</strong><small>${escapeHtml(row.nick || '')} · ${escapeHtml(row.core || '')} · ${Number(row.players) || 0} player(s)</small></div><span><code>${escapeHtml(address(row))}</code> <button type="button" class="icon-button" data-netplay-copy="${escapeHtml(address(row))}">Copy</button></span></div>`).join('') : '<p class="description">No netplay rooms are listed right now.</p>';
    target.querySelectorAll('[data-netplay-copy]').forEach(button => button.onclick = async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.netplayCopy);
        notify(t('notify.netplay.copied'));
      } catch (error) { notify(error.message); }
    });
  } catch (error) {
    target.innerHTML = `<p class="description">${escapeHtml(error.message)}</p>`;
  }
}

function bindNetplay() {
  const button = $('netplayButton');
  if (button) button.onclick = openNetplay;
  const bigBoxButton = $('bigBoxNetplay');
  if (bigBoxButton) bigBoxButton.onclick = () => {
    const menu = $('bigBoxMenu');
    if (menu) menu.hidden = true;
    openNetplay();
  };
  const close = $('closeNetplay');
  if (close) close.onclick = () => $('netplayDialog').close();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindNetplay, { once: true });
else bindNetplay();

export { openNetplay };
