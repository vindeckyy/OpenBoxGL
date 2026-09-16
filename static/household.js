/* household.js — local-first family records, challenges, shared stats, and Now Playing presence. */
import { $, escapeHtml } from './util.js';
import { api, AppState, notify, notifyError } from './state.js';
import { t } from './i18n.js';
import { openDialog } from './dialogs.js';

const PRESENCE_TTL_MS = 10 * 60 * 1000;
const ACTIVITY_POLL_MS = 60 * 1000;
const HEARTBEAT_MS = 2 * 60 * 1000;

let dialog = null;
let content = null;
let current = null;
let activity = null;
let localPresence = { opted_in: {}, toast: false };
let activityTimer = null;
let heartbeatTimer = null;
const toastedMembers = new Map();

function countLabel(count, singular, plural) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function challengeStatusLabel(status) {
  const normalized = String(status || '').trim().toLowerCase();
  switch (normalized) {
    case 'open': return t('household.status_open');
    case 'active': return t('household.status_active');
    case 'closed': return t('household.status_closed');
    case 'cancelled': return t('household.status_cancelled');
    case 'completed': return t('household.status_completed');
    case 'complete': return t('household.status_complete');
    case 'expired': return t('household.status_expired');
    default: return status || t('household.status_open');
  }
}

function ensureDialog() {
  if (dialog) return dialog;
  dialog = document.createElement('dialog');
  dialog.id = 'householdDialog';
  dialog.className = 'household-dialog';
  dialog.setAttribute('aria-labelledby', 'householdTitle');
  dialog.innerHTML = `<div class="dialog-head"><h2 id="householdTitle">${escapeHtml(t('household.title'))}</h2><button type="button" class="icon-button" data-household-close aria-label="${escapeHtml(t('household.close'))}">×</button></div><div class="form-grid"><div class="household-activity wide" data-household-activity></div><p class="description wide" data-household-status></p><div class="household-content wide"></div><div class="dialog-actions wide"><button type="button" class="icon-button" data-household-export>${escapeHtml(t('household.export'))}</button><button type="button" class="icon-button" data-household-refresh>${escapeHtml(t('household.refresh'))}</button><button type="button" class="primary" data-household-close>${escapeHtml(t('household.done'))}</button></div></div>`;
  document.body.appendChild(dialog);
  content = dialog.querySelector('.household-content');
  dialog.querySelectorAll('[data-household-close]').forEach(button => button.onclick = closeHousehold);
  dialog.querySelector('[data-household-refresh]').onclick = () => { refreshHousehold(); refreshActivity(); };
  dialog.querySelector('[data-household-export]').onclick = exportRecords;
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeHousehold(); });
  return dialog;
}

function statusText(text) {
  const element = dialog?.querySelector('[data-household-status]');
  if (element) element.textContent = text;
}

function memberRows(records) {
  return records.filter(record => record.kind === 'member' && record.payload).map(record => record.payload);
}

function memberName(memberId) {
  const member = memberRows(current?.records || []).find(item => item.member_id === memberId);
  return member?.display_name || memberId;
}

function challengeForm(challenge) {
  return `<form class="household-result-form" data-challenge-id="${escapeHtml(challenge.challenge_id)}"><input name="member_id" required placeholder="${escapeHtml(t('household.member_id'))}" aria-label="${escapeHtml(t('household.member_id'))}"><input name="value" type="number" min="0" value="0" aria-label="${escapeHtml(t('household.progress_value'))}"><button type="submit" class="icon-button">${escapeHtml(t('household.record_progress'))}</button></form>`;
}

function elapsedText(seconds) {
  const minutes = Math.max(0, Math.floor(Number(seconds || 0) / 60));
  if (minutes < 60) return t('household.activity_elapsed', { minutes: String(minutes) });
  return t('household.activity_elapsed_hours', { hours: (minutes / 60).toFixed(1) });
}

function presenceSection(members) {
  const opted = localPresence.opted_in || {};
  const options = members
    .map(member => `<option value="${escapeHtml(member.member_id)}">${escapeHtml(member.display_name)}</option>`)
    .join('');
  const selected = Object.keys(opted)[0] || members[0]?.member_id || '';
  const optionsMarkup = options || `<option value="">${escapeHtml(t('household.no_members'))}</option>`;
  return `<section class="detail-card household-section"><h3>${escapeHtml(t('household.presence_title'))}</h3><p class="description">${escapeHtml(t('household.presence_off'))}</p><label class="field"><span>${escapeHtml(t('household.presence_member'))}</span><select name="presence_member"${members.length ? '' : ' disabled'}>${optionsMarkup}</select></label><label class="check"><input type="checkbox" name="presence_share"${selected && opted[selected] ? ' checked' : ''}${members.length ? '' : ' disabled'}> <span>${escapeHtml(t('household.presence_share'))}</span></label><label class="check"><input type="checkbox" name="presence_toast"${localPresence.toast ? ' checked' : ''}${members.length ? '' : ' disabled'}> <span>${escapeHtml(t('household.presence_toast'))}</span></label><p class="description" data-presence-status></p></section>`;
}

function renderActivityStrip() {
  const strip = dialog?.querySelector('[data-household-activity]');
  if (!strip) return;
  const members = activity?.members || [];
  if (activity && activity.available === false) {
    strip.innerHTML = `<div class="household-live household-live-empty"><h3>${escapeHtml(t('household.activity_title'))}</h3><p class="description">${escapeHtml(t('household.activity_unavailable'))}</p></div>`;
    return;
  }
  if (!members.length) {
    strip.innerHTML = `<div class="household-live household-live-empty"><h3>${escapeHtml(t('household.activity_title'))}</h3><p class="description">${escapeHtml(t('household.activity_empty'))}</p></div>`;
    return;
  }
  strip.innerHTML = `<div class="household-live"><h3>${escapeHtml(t('household.activity_title'))}</h3><ul class="household-live-list">${members.map(member => {
    const game = member.game_name || member.platform || t('household.activity_idle');
    const join = member.join_hint === 'game-night'
      ? `<button type="button" class="icon-button" data-activity-join="${escapeHtml(member.member_id)}">${escapeHtml(t('household.activity_join'))}</button>`
      : '';
    return `<li class="household-live-member"><span class="household-live-dot" aria-hidden="true"></span><div><strong>${escapeHtml(member.display_name)}</strong><span class="description">${escapeHtml(game)} · ${escapeHtml(elapsedText(member.elapsed_seconds))}</span></div>${join}</li>`;
  }).join('')}</ul></div>`;
  strip.querySelectorAll('[data-activity-join]').forEach(button => {
    button.onclick = () => {
      import('./party.js').then(module => module.openParty()).catch(() => {});
    };
  });
}

function maybeToast() {
  if (!localPresence.toast) return;
  const now = Date.now();
  for (const member of activity?.members || []) {
    const key = `${member.device_id}:${member.member_id}`;
    if (now - (toastedMembers.get(key) || 0) < PRESENCE_TTL_MS) continue;
    toastedMembers.set(key, now);
    notify('info', t('household.activity_toast', {
      name: member.display_name,
      game: member.game_name || member.platform || t('household.activity_idle'),
    }));
  }
}

async function refreshActivity() {
  try {
    activity = await api('/api/v2/household/activity');
    localPresence = activity.presence || localPresence;
    renderActivityStrip();
    maybeToast();
  } catch {
    activity = null;
    renderActivityStrip();
  }
}

async function sendHeartbeat() {
  const running = (AppState.runningGames || [])[0];
  if (!running) return;
  const memberId = Object.keys(localPresence.opted_in || {})[0];
  if (!memberId) return;
  const game = (AppState.games || []).find(item => item.id === running.game_id) || {};
  const partyOpen = Boolean($('partyOverlay') && !$('partyOverlay').hidden);
  try {
    await api('/api/v2/household/presence/heartbeat', {
      method: 'POST',
      body: JSON.stringify({
        member_id: memberId,
        display_name: memberName(memberId),
        game_id: String(game.game_id || running.game_id || ''),
        game_name: running.game || game.name || '',
        platform: game.platform || '',
        started_at: running.started || null,
        game_night: partyOpen ? { players: Number(AppState.appSettings.party_players) || 2 } : null,
      }),
    });
  } catch { /* presence is best-effort; never surface a heartbeat failure */ }
}

function startPresenceLoop() {
  if (activityTimer) return;
  refreshActivity();
  sendHeartbeat();
  activityTimer = window.setInterval(() => { refreshActivity(); sendHeartbeat(); }, ACTIVITY_POLL_MS);
  heartbeatTimer = window.setInterval(sendHeartbeat, HEARTBEAT_MS);
}

function presenceStatus(message) {
  const element = content?.querySelector('[data-presence-status]');
  if (element) element.textContent = message;
}

async function savePresence(overrides = {}) {
  const memberId = content?.querySelector('[name="presence_member"]')?.value || '';
  if (!memberId) return;
  const enabled = overrides.enabled ?? Boolean(content?.querySelector('[name="presence_share"]')?.checked);
  const toast = overrides.toast ?? Boolean(content?.querySelector('[name="presence_toast"]')?.checked);
  try {
    const result = await api('/api/v2/household/presence', {
      method: 'POST',
      body: JSON.stringify({ member_id: memberId, enabled, toast }),
    });
    localPresence = result.presence || localPresence;
    presenceStatus(t('household.presence_saved'));
    if (enabled) sendHeartbeat();
  } catch (error) {
    presenceStatus('');
    notifyError(error);
  }
}

function render(data, notice = '') {
  current = data;
  AppState.household = data;
  if (data.presence) localPresence = data.presence;
  const members = memberRows(data.records || []);
  const challenges = data.challenges || [];
  const entries = data.leaderboard?.entries || [];
  const sync = data.household_sync || {};
  const syncConfigured = sync.configured === true;
  const syncLabel = syncConfigured
    ? t('household.sync_available')
    : t('household.sync_configure');
  const syncDisabled = syncConfigured ? '' : ' disabled';
  const memberMarkup = members.length
    ? members.map(member => `<li><strong>${escapeHtml(member.display_name)}</strong><span class="description">${escapeHtml(member.member_id)} · ${escapeHtml(member.avatar_color || t('household.avatar_default'))}</span></li>`).join('')
    : `<li class="description">${escapeHtml(t('household.no_members'))}</li>`;
  const challengeMarkup = challenges.length
    ? challenges.map(challenge => `<article class="detail-card household-challenge"><div class="dialog-head"><strong>${escapeHtml(challenge.challenge_id)}</strong><span class="description">${escapeHtml(challengeStatusLabel(challenge.status))}</span></div><p>${escapeHtml(String(challenge.current ?? 0))} / ${escapeHtml(String(challenge.target ?? 0))} · ${escapeHtml(String(challenge.percent ?? 0))}%</p>${challengeForm(challenge)}</article>`).join('')
    : `<p class="description">${escapeHtml(t('household.no_challenges'))}</p>`;
  const leaderboardMarkup = data.leaderboard?.available && entries.length
    ? `<ol>${entries.map(entry => `<li><strong>#${escapeHtml(String(entry.rank))} ${escapeHtml(entry.display_name)}</strong><span class="description">${escapeHtml(String(entry.completions || 0))} ${escapeHtml(t('household.completions'))} · ${escapeHtml(String(entry.playtime_seconds || 0))} ${escapeHtml(t('household.seconds'))}</span></li>`).join('')}</ol>`
    : `<p class="description">${escapeHtml(data.stats_sharing ? t('household.no_shared_stats') : t('household.stats_sharing_off'))}</p>`;
  content.innerHTML = `<section class="detail-card household-section"><h3>${escapeHtml(t('household.shared_folder'))}</h3><p class="description">${escapeHtml(syncLabel)}</p><div class="dialog-actions"><button type="button" class="icon-button" data-household-publish${syncDisabled}>${escapeHtml(t('household.publish_pending'))}</button><button type="button" class="icon-button" data-household-pull${syncDisabled}>${escapeHtml(t('household.pull_shared'))}</button></div></section>${presenceSection(members)}<section class="detail-card household-section"><h3>${escapeHtml(t('household.members'))}</h3><ul>${memberMarkup}</ul><form class="household-member-form"><input name="member_id" required placeholder="${escapeHtml(t('household.member_id'))}" aria-label="${escapeHtml(t('household.member_id'))}"><input name="display_name" required placeholder="${escapeHtml(t('household.display_name'))}" aria-label="${escapeHtml(t('household.display_name'))}"><input name="avatar_color" value="blue" placeholder="${escapeHtml(t('household.avatar_color'))}" aria-label="${escapeHtml(t('household.avatar_color'))}"><button type="submit" class="primary">${escapeHtml(t('household.add_member'))}</button></form></section><section class="detail-card household-section"><h3>${escapeHtml(t('household.challenges'))}</h3><div>${challengeMarkup}</div><form class="household-challenge-form"><input name="title" required placeholder="${escapeHtml(t('household.challenge_title'))}" aria-label="${escapeHtml(t('household.challenge_title'))}"><input name="target" type="number" min="1" value="1" aria-label="${escapeHtml(t('household.challenge_target'))}"><input name="created_by" placeholder="${escapeHtml(t('household.created_by'))}" aria-label="${escapeHtml(t('household.created_by'))}"><button type="submit" class="primary">${escapeHtml(t('household.create_challenge'))}</button></form></section><section class="detail-card household-section"><h3>${escapeHtml(t('household.leaderboard'))}</h3>${leaderboardMarkup}</section>`;
  content.querySelector('[data-household-publish]')?.addEventListener('click', publishHousehold);
  content.querySelector('[data-household-pull]')?.addEventListener('click', pullHousehold);
  content.querySelector('.household-member-form')?.addEventListener('submit', submitMember);
  content.querySelector('.household-challenge-form')?.addEventListener('submit', submitChallenge);
  content.querySelectorAll('.household-result-form').forEach(form => form.addEventListener('submit', submitResult));
  content.querySelector('[name="presence_share"]')?.addEventListener('change', () => savePresence());
  content.querySelector('[name="presence_toast"]')?.addEventListener('change', () => savePresence({ toast: Boolean(content.querySelector('[name="presence_toast"]').checked) }));
  content.querySelector('[name="presence_member"]')?.addEventListener('change', event => {
    const selected = event.target.value;
    const share = Boolean(content.querySelector('[name="presence_share"]'));
    share.checked = Boolean(localPresence.opted_in?.[selected]);
  });
  renderActivityStrip();
  const memberCount = countLabel(members.length, t('household.member_singular'), t('household.member_plural'));
  const challengeCount = countLabel(challenges.length, t('household.challenge_singular'), t('household.challenge_plural'));
  const recordCount = countLabel(data.outbox_count || 0, t('household.local_record_singular'), t('household.local_record_plural'));
  statusText(notice || t('household.status', { members: memberCount, challenges: challengeCount, records: recordCount }));
}

async function refreshHousehold() {
  ensureDialog();
  statusText(t('household.loading'));
  try {
    const data = await api('/api/v2/household');
    localPresence = data.presence || localPresence;
    render(data);
    refreshActivity();
  } catch (error) {
    statusText(error.message || t('household.unavailable'));
    notifyError(error);
  }
}

async function submitMember(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    render(await api('/api/v2/household/member', { method: 'POST', body: JSON.stringify(values) }));
  } catch (error) { notifyError(error); }
}

async function submitChallenge(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  values.target = Number(values.target);
  try {
    render(await api('/api/v2/household/challenge', { method: 'POST', body: JSON.stringify(values) }));
  } catch (error) { notifyError(error); }
}

async function submitResult(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  values.challenge_id = event.currentTarget.dataset.challengeId;
  values.value = Number(values.value);
  try {
    render(await api('/api/v2/household/challenge/result', { method: 'POST', body: JSON.stringify(values) }));
  } catch (error) { notifyError(error); }
}

async function publishHousehold() {
  statusText(t('household.publishing'));
  try {
    const data = await api('/api/v2/household/sync/publish', { method: 'POST', body: '{}' });
    const result = data.sync || {};
    const skipped = result.stats_skipped
      ? ` · ${result.stats_skipped === 1
        ? t('household.stats_skipped_one', { count: result.stats_skipped })
        : t('household.stats_skipped_many', { count: result.stats_skipped })}`
      : '';
    render(data, t('household.published', {
      published: result.published || 0,
      record_label: result.published === 1 ? t('household.record_singular') : t('household.record_plural'),
      acknowledged: result.acknowledged || 0,
      skipped,
    }));
  } catch (error) { notifyError(error); }
}

async function pullHousehold() {
  statusText(t('household.pulling'));
  try {
    const data = await api('/api/v2/household/sync/pull', { method: 'POST', body: '{}' });
    const result = data.sync || {};
    render(data, t('household.pulled', {
      pulled: result.pulled || 0,
      record_label: result.pulled === 1 ? t('household.shared_record_singular') : t('household.shared_record_plural'),
      applied: result.applied || 0,
    }));
  } catch (error) { notifyError(error); }
}

function exportRecords() {
  if (!current) return;
  const blob = new Blob([JSON.stringify({ format: current.format, records: current.records }, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'openbox-household-records.json';
  link.click();
  URL.revokeObjectURL(url);
}

export function openHousehold() {
  ensureDialog();
  if (!dialog.open) openDialog(dialog);
  refreshHousehold();
}

function closeHousehold() {
  if (dialog?.open) dialog.close();
}

export function initHousehold() {
  document.addEventListener('app:open-household', openHousehold);
  document.addEventListener('localechange', () => {
    if (dialog?.open) render(current || {}, '');
    renderActivityStrip();
  });
  AppState.household = current;
  startPresenceLoop();
}
