/* Player's Log — Time Capsules & Deterministic Milestone Cards (ADR 0110). */
import { $, escapeHtml } from './util.js';
import { api, notify } from './state.js';
import { t } from './i18n.js';
import { openDialog, closeDialog, confirmAction } from './dialogs.js';
import { renderShareCard, downloadCanvasCard } from './moments.js';

function localTodayIso() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function defaultFutureIso(daysAhead = 30) {
  const target = new Date(Date.now() + daysAhead * 86400 * 1000);
  const y = target.getFullYear();
  const m = String(target.getMonth() + 1).padStart(2, '0');
  const d = String(target.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function showOpenedCapsuleDialog(cap) {
  const dialog = document.createElement('dialog');
  dialog.innerHTML = `
    <div class="dialog-head">
      <h2>${escapeHtml(cap.title || t('playerlog.capsule_opened'))}</h2>
      <button type="button" class="icon-button" data-close aria-label="${escapeHtml(t('common.close'))}">×</button>
    </div>
    <p class="description">${escapeHtml([cap.game_name, cap.unlock_at].filter(Boolean).join(' · '))}</p>
    <div class="detail-card"><p>${escapeHtml(cap.body || '')}</p></div>
    <div class="dialog-actions"><button type="button" class="primary" data-close>${escapeHtml(t('common.close'))}</button></div>
  `;
  const cleanup = () => { closeDialog(dialog); dialog.remove(); };
  dialog.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', cleanup));
  dialog.addEventListener('cancel', cleanup, { once: true });
  document.body.appendChild(dialog);
  openDialog(dialog);
}

function openSealCapsuleDialog({ gameId = '', gameName = '', onCreated = null } = {}) {
  const dialog = document.createElement('dialog');
  const minDate = localTodayIso();
  const defaultUnlock = defaultFutureIso(30);
  dialog.innerHTML = `
    <div class="dialog-head">
      <h2>${escapeHtml(t('playerlog.seal_dialog_title'))}</h2>
      <button type="button" class="icon-button" data-close aria-label="${escapeHtml(t('common.close'))}">×</button>
    </div>
    <form class="form" id="sealCapsuleForm">
      ${gameName ? `<p class="description"><strong>${escapeHtml(gameName)}</strong></p>` : ''}
      <p class="description">${escapeHtml(t('playerlog.seal_honesty'))}</p>
      <label class="field">
        <span>${escapeHtml(t('playerlog.field_title'))}</span>
        <input name="title" maxlength="120" required value="${escapeHtml(gameName || '')}">
      </label>
      <label class="field">
        <span>${escapeHtml(t('playerlog.field_kind'))}</span>
        <select name="kind">
          <option value="note">${escapeHtml(t('playerlog.kind_note'))}</option>
          <option value="question">${escapeHtml(t('playerlog.kind_question'))}</option>
        </select>
      </label>
      <label class="field">
        <span>${escapeHtml(t('playerlog.field_unlock_at'))}</span>
        <input name="unlock_at" type="date" required value="${escapeHtml(defaultUnlock)}" min="${escapeHtml(minDate)}">
      </label>
      <label class="field">
        <span>${escapeHtml(t('playerlog.field_body'))}</span>
        <textarea name="body" rows="5" maxlength="4000" required></textarea>
      </label>
      <div class="dialog-actions">
        <button type="button" class="icon-button" data-close>${escapeHtml(t('common.cancel'))}</button>
        <button type="submit" class="primary">${escapeHtml(t('playerlog.seal_submit'))}</button>
      </div>
    </form>
  `;
  const cleanup = () => { closeDialog(dialog); dialog.remove(); };
  dialog.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', cleanup));
  dialog.addEventListener('cancel', cleanup, { once: true });
  dialog.querySelector('#sealCapsuleForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      game_id: gameId || '',
      game_name: gameName || '',
      title: form.title.value.trim(),
      kind: form.kind.value,
      unlock_at: form.unlock_at.value,
      body: form.body.value,
    };
    try {
      const res = await api('/api/v2/capsules', { method: 'POST', body: JSON.stringify(payload) });
      notify(t('playerlog.capsule_sealed', { date: res.capsule?.unlock_at || payload.unlock_at }));
      cleanup();
      if (onCreated) await onCreated(res.capsule);
    } catch (error) {
      notify(error.message || String(error));
    }
  });
  document.body.appendChild(dialog);
  openDialog(dialog);
}

async function shareMilestoneCard(milestone) {
  try {
    const canvas = await renderShareCard({
      kicker: t('playerlog.milestone_kicker'),
      title: milestone.title || '',
      subtitle: `${milestone.unlocked_at || ''}${milestone.approximate ? ` · ${t('playerlog.approx_date')}` : ''}`,
      body: milestone.subtitle || '',
      footer: milestone.game_name || 'OpenBox',
    });
    await downloadCanvasCard(canvas, `openbox-${String(milestone.id || 'milestone').replace(/[^A-Za-z0-9_-]+/g, '-')}.png`, { preferClipboard: false });
  } catch (error) {
    notify(error.message || t('moments.share_unavailable'));
  }
}

function renderBanner(nextData) {
  const due = Number(nextData?.due_count || 0);
  const cap = nextData?.capsule;
  const days = nextData?.days_until;
  if (due > 0) {
    return `<div class="detail-card playerlog-banner"><strong>${escapeHtml(t(due === 1 ? 'playerlog.banner_due_one' : 'playerlog.banner_due', { count: due }))}</strong></div>`;
  }
  if (cap && typeof days === 'number' && days <= 7) {
    return `<div class="detail-card playerlog-banner"><span>${escapeHtml(t('playerlog.banner_upcoming', { title: cap.title || '', days, date: cap.unlock_at || '' }))}</span></div>`;
  }
  return '';
}

function renderMilestonesBlock(data) {
  const unlocked = Array.isArray(data?.unlocked) ? data.unlocked : [];
  const upcoming = Array.isArray(data?.upcoming) ? data.upcoming : [];
  const newCount = Number(data?.counts?.new || 0);
  const cardsHtml = unlocked.length
    ? `<div class="playerlog-grid">${unlocked.slice(0, 12).map(m => `
        <article class="detail-card" data-milestone-id="${escapeHtml(m.id)}">
          <div class="backlog-controls">
            <strong>${escapeHtml(m.title)}</strong>
            ${m.is_new ? `<span class="badge">${escapeHtml(t('playerlog.new_badge'))}</span>` : ''}
          </div>
          <p class="description">${escapeHtml(m.subtitle || '')}</p>
          <div class="extras">
            <small class="description">${escapeHtml(m.unlocked_at || '')}${m.approximate ? ` · ${escapeHtml(t('playerlog.approx_date'))}` : ''}</small>
            <button type="button" class="icon-button" data-share-milestone="${escapeHtml(m.id)}">${escapeHtml(t('playerlog.share_card'))}</button>
          </div>
        </article>`).join('')}</div>`
    : `<p class="description">${escapeHtml(t('playerlog.milestones_empty'))}</p>`;

  const upcomingHtml = upcoming.length
    ? `<div class="backlog-rows"><h4>${escapeHtml(t('playerlog.upcoming_title'))}</h4>${upcoming.slice(0, 4).map(u => `
        <div class="backlog-row">
          <span class="grow">${escapeHtml(u.title)}</span>
          <span class="description">${escapeHtml(String(u.current))} / ${escapeHtml(String(u.target))} (${escapeHtml(String(u.progress_pct))}%)</span>
        </div>`).join('')}</div>`
    : '';

  return `
    <div class="insight-section">
      <div class="backlog-controls">
        <h3 class="insight-title">${escapeHtml(t('playerlog.milestones_title'))}</h3>
        ${newCount > 0 ? `<button type="button" class="icon-button" id="playerlogMarkSeen">${escapeHtml(t('playerlog.mark_seen'))}</button>` : ''}
      </div>
      ${cardsHtml}
      ${upcomingHtml}
    </div>
  `;
}

function renderCapsulesBlock(capData) {
  const items = Array.isArray(capData?.capsules) ? capData.capsules : [];
  const rowsHtml = items.length
    ? items.map(c => {
      const canOpen = !c.opened_at && c.due;
      const status = c.opened_at
        ? t('playerlog.unlocked_label', { date: c.unlock_at || '' })
        : t('playerlog.sealed_until', { date: c.unlock_at || '' });
      return `
        <div class="backlog-row" data-capsule-id="${escapeHtml(c.id)}">
          <div class="grow">
            <strong>${escapeHtml(c.title)}</strong>
            ${c.game_name ? ` · <span class="badge">${escapeHtml(c.game_name)}</span>` : ''}
            <small class="description">${escapeHtml(status)}</small>
          </div>
          ${c.opened_at ? `<button type="button" class="icon-button" data-capsule-view="${escapeHtml(c.id)}">${escapeHtml(t('playerlog.view_capsule'))}</button>` : ''}
          ${canOpen ? `<button type="button" class="primary" data-capsule-open="${escapeHtml(c.id)}">${escapeHtml(t('playerlog.open_capsule'))}</button>` : ''}
          <button type="button" class="icon-button" data-capsule-delete="${escapeHtml(c.id)}">${escapeHtml(t('playerlog.delete_capsule'))}</button>
        </div>
      `;
    }).join('')
    : `<p class="description">${escapeHtml(t('playerlog.capsules_empty'))}</p>`;

  return `
    <div class="insight-section">
      <div class="backlog-controls">
        <h3 class="insight-title">${escapeHtml(t('playerlog.capsules_title'))}</h3>
        <button type="button" class="primary" id="playerlogSealMemory">${escapeHtml(t('playerlog.seal_memory'))}</button>
      </div>
      ${rowsHtml}
    </div>
  `;
}

async function mountPlayerLog(container, { markSeen = false } = {}) {
  if (!container) return;
  try {
    const query = markSeen ? '?mark_seen=1' : '';
    const [milestonesRes, capsulesRes, nextRes] = await Promise.all([
      api(`/api/v2/insights/milestones${query}`),
      api('/api/v2/capsules'),
      api('/api/v2/capsules/next'),
    ]);
    container.innerHTML = `
      <div class="insight-section" id="playerLogRoot">
        <h3 class="insight-title">${escapeHtml(t('playerlog.title'))}</h3>
        ${renderBanner(nextRes)}
        ${renderMilestonesBlock(milestonesRes)}
        ${renderCapsulesBlock(capsulesRes)}
      </div>
    `;
    const unlockedMap = new Map((milestonesRes.unlocked || []).map(item => [item.id, item]));
    const capsuleMap = new Map((capsulesRes.capsules || []).map(item => [item.id, item]));

    container.querySelector('#playerlogMarkSeen')?.addEventListener('click', () => mountPlayerLog(container, { markSeen: true }));
    container.querySelector('#playerlogSealMemory')?.addEventListener('click', () => {
      openSealCapsuleDialog({ onCreated: () => mountPlayerLog(container) });
    });
    container.querySelectorAll('[data-share-milestone]').forEach(btn => {
      btn.addEventListener('click', () => {
        const milestone = unlockedMap.get(btn.dataset.shareMilestone);
        if (milestone) shareMilestoneCard(milestone);
      });
    });
    container.querySelectorAll('[data-capsule-open]').forEach(btn => {
      btn.addEventListener('click', async () => {
        try {
          const res = await api('/api/v2/capsules/open', { method: 'POST', body: JSON.stringify({ id: btn.dataset.capsuleOpen }) });
          if (res.capsule) showOpenedCapsuleDialog(res.capsule);
          await mountPlayerLog(container);
        } catch (error) {
          notify(error.message || String(error));
        }
      });
    });
    container.querySelectorAll('[data-capsule-view]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cap = capsuleMap.get(btn.dataset.capsuleView);
        if (cap) showOpenedCapsuleDialog(cap);
      });
    });
    container.querySelectorAll('[data-capsule-delete]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ok = await confirmAction({
          title: t('playerlog.delete_title'),
          message: t('playerlog.delete_message'),
          confirmLabel: t('playerlog.delete_capsule'),
          destructive: true,
        });
        if (!ok) return;
        try {
          await api('/api/v2/capsules/delete', { method: 'POST', body: JSON.stringify({ id: btn.dataset.capsuleDelete }) });
          notify(t('playerlog.capsule_deleted'));
          await mountPlayerLog(container);
        } catch (error) {
          notify(error.message || String(error));
        }
      });
    });
  } catch {
    container.innerHTML = '';
  }
}

export { mountPlayerLog, openSealCapsuleDialog };
