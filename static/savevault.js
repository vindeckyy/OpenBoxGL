/* Save Vault UI panel inside the Save History dialog and game detail saves card (ADR 0107). */
import { escapeHtml } from './util.js';
import { api, gameIdOf, notify, waitForJob } from './state.js';
import { t } from './i18n.js';
import { confirmAction } from './dialogs.js';

function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

async function fetchVaultForGame(id) {
  const gid = gameIdOf(id);
  const query = gid ? `game_id=${encodeURIComponent(gid)}` : `id=${encodeURIComponent(id)}`;
  return api(`/api/v2/saves/vault?${query}`);
}

async function renderVaultSummaryBadge(slot, id) {
  if (!slot) return;
  try {
    const gid = gameIdOf(id);
    const query = gid ? `game_id=${encodeURIComponent(gid)}` : `id=${encodeURIComponent(id)}`;
    const status = await api(`/api/v2/saves/vault/status?${query}`);
    if (status.enabled === false) {
      slot.textContent = '';
      return;
    }
    const count = Number(status.game_snapshots || 0);
    const bytes = Number(status.game_bytes || 0);
    slot.textContent = count
      ? t('vault.summary_count', { count, size: formatBytes(bytes) })
      : t('vault.summary_empty');
  } catch {
    slot.textContent = '';
  }
}

async function renderSaveVaultPanel(container, id, { onChanged = null } = {}) {
  if (!container) return;
  const gid = gameIdOf(id);
  container.innerHTML = `<p class="description">${escapeHtml(t('common.loading'))}</p>`;
  let payload;
  try {
    payload = await fetchVaultForGame(id);
  } catch (error) {
    container.innerHTML = `<p class="description">${escapeHtml(error.message || String(error))}</p>`;
    return;
  }
  if (payload.enabled === false) {
    container.innerHTML = `<p class="description">${escapeHtml(t('vault.disabled'))}</p>`;
    return;
  }
  const snapshots = Array.isArray(payload.snapshots) ? payload.snapshots : [];
  const status = payload.status || {};
  const warn = status.over_5gb_warning
    ? `<p class="description"><strong>${escapeHtml(t('vault.disk_warning', { size: formatBytes(status.bytes) }))}</strong></p>`
    : '';
  const rowsHtml = snapshots.length
    ? snapshots.map(snap => {
      const when = escapeHtml(String(snap.created_at || '').replace('T', ' ').replace('Z', ' UTC'));
      const trig = escapeHtml(snap.trigger || 'manual');
      return `<div class="backlog-row" data-vault-row="${escapeHtml(snap.id)}">
        <div class="grow">
          <strong>${when}</strong> · <span class="badge">${trig}</span>
          <small class="description">${escapeHtml(t('vault.row_meta', { files: snap.files || 0, size: formatBytes(snap.bytes) }))}</small>
        </div>
        <button type="button" class="icon-button" data-vault-diff="${escapeHtml(snap.id)}">${escapeHtml(t('vault.diff'))}</button>
        <button type="button" class="icon-button" data-vault-restore="${escapeHtml(snap.id)}">${escapeHtml(t('vault.restore'))}</button>
        <button type="button" class="icon-button" data-vault-delete="${escapeHtml(snap.id)}">${escapeHtml(t('vault.delete'))}</button>
      </div>`;
    }).join('')
    : `<p class="description">${escapeHtml(t('vault.empty'))}</p>`;

  container.innerHTML = `
    <div class="extras">
      <button type="button" class="primary" id="vaultSnapshotNow">${escapeHtml(t('vault.snapshot_now'))}</button>
      <span class="description">${escapeHtml(t('vault.total_usage', { count: status.snapshots || 0, size: formatBytes(status.bytes || 0) }))}</span>
    </div>
    ${warn}
    <div id="vaultActionStatus" class="description"></div>
    <div class="history-list" id="vaultSnapshotList">${rowsHtml}</div>
    <div id="vaultDiffPreview" class="detail-card" hidden></div>
  `;

  const reload = async () => {
    await renderSaveVaultPanel(container, id, { onChanged });
    if (onChanged) onChanged();
  };

  container.querySelector('#vaultSnapshotNow')?.addEventListener('click', async () => {
    const statusEl = container.querySelector('#vaultActionStatus');
    try {
      const queued = await api('/api/v2/saves/vault/snapshot', {
        method: 'POST',
        body: JSON.stringify({ id, game_id: gid, trigger: 'manual' }),
      });
      if (statusEl) statusEl.textContent = t('vault.snapshotting');
      const job = await waitForJob(queued.job_id);
      if (job?.state === 'error') throw new Error(job.error?.message || String(job.error || 'Snapshot failed'));
      notify(t('vault.snapshot_created'));
      await reload();
    } catch (error) {
      if (statusEl) statusEl.textContent = error.message || String(error);
      notify(error.message || String(error));
    }
  });

  container.querySelectorAll('[data-vault-diff]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const snapId = btn.dataset.vaultDiff;
      const box = container.querySelector('#vaultDiffPreview');
      if (!box) return;
      box.hidden = false;
      box.innerHTML = `<p class="description">${escapeHtml(t('common.loading'))}</p>`;
      try {
        const q = gid ? `game_id=${encodeURIComponent(gid)}` : `id=${encodeURIComponent(id)}`;
        const diff = await api(`/api/v2/saves/vault/diff?${q}&snapshot_id=${encodeURIComponent(snapId)}`);
        const c = diff.counts || {};
        const changed = (diff.changed || []).slice(0, 8).map(item => `<li><code>${escapeHtml(item.relpath || item.path)}</code></li>`).join('');
        box.innerHTML = `
          <h4>${escapeHtml(t('vault.diff_title'))}</h4>
          <p class="description">${escapeHtml(t('vault.diff_counts', { added: c.added || 0, changed: c.changed || 0, removed: c.removed || 0, unchanged: c.unchanged || 0 }))}</p>
          ${changed ? `<ul>${changed}</ul>` : ''}
        `;
      } catch (error) {
        box.innerHTML = `<p class="description">${escapeHtml(error.message || String(error))}</p>`;
      }
    });
  });

  container.querySelectorAll('[data-vault-restore]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const snapId = btn.dataset.vaultRestore;
      const statusEl = container.querySelector('#vaultActionStatus');
      try {
        const dry = await api('/api/v2/saves/vault/restore', {
          method: 'POST',
          body: JSON.stringify({ id, game_id: gid, snapshot_id: snapId, dry_run: true }),
        });
        const c = dry.diff?.counts || {};
        const ok = await confirmAction({
          title: t('vault.restore_title'),
          message: t('vault.restore_message', { added: c.added || 0, changed: c.changed || 0 }),
          consequence: t('vault.restore_consequence'),
          confirmLabel: t('vault.restore'),
        });
        if (!ok) return;
        const queued = await api('/api/v2/saves/vault/restore', {
          method: 'POST',
          body: JSON.stringify({ id, game_id: gid, snapshot_id: snapId, dry_run: false }),
        });
        if (statusEl) statusEl.textContent = t('vault.restoring');
        const job = await waitForJob(queued.job_id);
        if (job?.state === 'error') throw new Error(job.error?.message || String(job.error || 'Restore failed'));
        notify(t('vault.restored'));
        await reload();
      } catch (error) {
        if (statusEl) statusEl.textContent = error.message || String(error);
        notify(error.message || String(error));
      }
    });
  });

  container.querySelectorAll('[data-vault-delete]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const snapId = btn.dataset.vaultDelete;
      const ok = await confirmAction({
        title: t('vault.delete_title'),
        message: t('vault.delete_message'),
        consequence: t('vault.delete_consequence'),
        confirmLabel: t('vault.delete'),
      });
      if (!ok) return;
      try {
        await api('/api/v2/saves/vault/delete', {
          method: 'POST',
          body: JSON.stringify({ id, game_id: gid, snapshot_id: snapId }),
        });
        notify(t('vault.deleted'));
        await reload();
      } catch (error) {
        notify(error.message || String(error));
      }
    });
  });
}

export { renderSaveVaultPanel, renderVaultSummaryBadge };
