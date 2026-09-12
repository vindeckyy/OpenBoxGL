/* household.js — local-first family records, challenges, and shared stats. */
import { $, escapeHtml } from './util.js';
import { api, AppState, notify } from './state.js';
import { t } from './i18n.js';

let dialog = null;
let content = null;
let current = null;

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
  dialog.innerHTML = `<div class="dialog-head"><h2 id="householdTitle">${escapeHtml(t('household.title'))}</h2><button type="button" class="icon-button" data-household-close aria-label="${escapeHtml(t('household.close'))}">×</button></div><div class="form-grid"><p class="description wide" data-household-status></p><div class="household-content wide"></div><div class="dialog-actions wide"><button type="button" class="icon-button" data-household-export>${escapeHtml(t('household.export'))}</button><button type="button" class="icon-button" data-household-refresh>${escapeHtml(t('household.refresh'))}</button><button type="button" class="primary" data-household-close>${escapeHtml(t('household.done'))}</button></div></div>`;
  document.body.appendChild(dialog);
  content = dialog.querySelector('.household-content');
  dialog.querySelectorAll('[data-household-close]').forEach(button => button.onclick = closeHousehold);
  dialog.querySelector('[data-household-refresh]').onclick = () => refreshHousehold();
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

function challengeForm(challenge) {
  return `<form class="household-result-form" data-challenge-id="${escapeHtml(challenge.challenge_id)}"><input name="member_id" required placeholder="${escapeHtml(t('household.member_id'))}" aria-label="${escapeHtml(t('household.member_id'))}"><input name="value" type="number" min="0" value="0" aria-label="${escapeHtml(t('household.progress_value'))}"><button type="submit" class="icon-button">${escapeHtml(t('household.record_progress'))}</button></form>`;
}

function render(data, notice = '') {
  current = data;
  AppState.household = data;
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
  content.innerHTML = `<section class="detail-card household-section"><h3>${escapeHtml(t('household.shared_folder'))}</h3><p class="description">${escapeHtml(syncLabel)}</p><div class="dialog-actions"><button type="button" class="icon-button" data-household-publish${syncDisabled}>${escapeHtml(t('household.publish_pending'))}</button><button type="button" class="icon-button" data-household-pull${syncDisabled}>${escapeHtml(t('household.pull_shared'))}</button></div></section><section class="detail-card household-section"><h3>${escapeHtml(t('household.members'))}</h3><ul>${memberMarkup}</ul><form class="household-member-form"><input name="member_id" required placeholder="${escapeHtml(t('household.member_id'))}" aria-label="${escapeHtml(t('household.member_id'))}"><input name="display_name" required placeholder="${escapeHtml(t('household.display_name'))}" aria-label="${escapeHtml(t('household.display_name'))}"><input name="avatar_color" value="blue" placeholder="${escapeHtml(t('household.avatar_color'))}" aria-label="${escapeHtml(t('household.avatar_color'))}"><button type="submit" class="primary">${escapeHtml(t('household.add_member'))}</button></form></section><section class="detail-card household-section"><h3>${escapeHtml(t('household.challenges'))}</h3><div>${challengeMarkup}</div><form class="household-challenge-form"><input name="title" required placeholder="${escapeHtml(t('household.challenge_title'))}" aria-label="${escapeHtml(t('household.challenge_title'))}"><input name="target" type="number" min="1" value="1" aria-label="${escapeHtml(t('household.challenge_target'))}"><input name="created_by" placeholder="${escapeHtml(t('household.created_by'))}" aria-label="${escapeHtml(t('household.created_by'))}"><button type="submit" class="primary">${escapeHtml(t('household.create_challenge'))}</button></form></section><section class="detail-card household-section"><h3>${escapeHtml(t('household.leaderboard'))}</h3>${leaderboardMarkup}</section>`;
  content.querySelector('[data-household-publish]')?.addEventListener('click', publishHousehold);
  content.querySelector('[data-household-pull]')?.addEventListener('click', pullHousehold);
  content.querySelector('.household-member-form')?.addEventListener('submit', submitMember);
  content.querySelector('.household-challenge-form')?.addEventListener('submit', submitChallenge);
  content.querySelectorAll('.household-result-form').forEach(form => form.addEventListener('submit', submitResult));
  const memberCount = countLabel(members.length, t('household.member_singular'), t('household.member_plural'));
  const challengeCount = countLabel(challenges.length, t('household.challenge_singular'), t('household.challenge_plural'));
  const recordCount = countLabel(data.outbox_count || 0, t('household.local_record_singular'), t('household.local_record_plural'));
  statusText(notice || t('household.status', { members: memberCount, challenges: challengeCount, records: recordCount }));
}

async function refreshHousehold() {
  ensureDialog();
  statusText(t('household.loading'));
  try {
    render(await api('/api/v2/household'));
  } catch (error) {
    statusText(error.message || t('household.unavailable'));
    notify(error.message);
  }
}

async function submitMember(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    render(await api('/api/v2/household/member', { method: 'POST', body: JSON.stringify(values) }));
  } catch (error) { notify(error.message); }
}

async function submitChallenge(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  values.target = Number(values.target);
  try {
    render(await api('/api/v2/household/challenge', { method: 'POST', body: JSON.stringify(values) }));
  } catch (error) { notify(error.message); }
}

async function submitResult(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  values.challenge_id = event.currentTarget.dataset.challengeId;
  values.value = Number(values.value);
  try {
    render(await api('/api/v2/household/challenge/result', { method: 'POST', body: JSON.stringify(values) }));
  } catch (error) { notify(error.message); }
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
  } catch (error) { notify(error.message); }
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
  } catch (error) { notify(error.message); }
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
  if (!dialog.open) dialog.showModal();
  refreshHousehold();
}

export function closeHousehold() {
  if (dialog?.open) dialog.close();
}

export function initHousehold() {
  document.addEventListener('app:open-household', openHousehold);
  AppState.household = current;
}
