/* What's New + rotating tips (S8). Keep the release surface local and dismissible. */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';

const TIPS = [
  'whats_new.tip_search',
  'whats_new.tip_clip',
  'whats_new.tip_time_machine',
  'whats_new.tip_big_box',
];

let dialog = null;
let content = null;
let tipIndex = 0;

function loadTipIndex() {
  try {
    const value = Number(localStorage.getItem('openbox-tip-index'));
    return Number.isInteger(value) && value >= 0 ? value % TIPS.length : 0;
  } catch { return 0; }
}

function saveTipIndex() {
  try { localStorage.setItem('openbox-tip-index', String(tipIndex)); } catch { /* storage unavailable */ }
}

function ensureDialog() {
  if (dialog) return dialog;
  dialog = document.createElement('dialog');
  dialog.id = 'whatsNewDialog';
  dialog.className = 'whats-new-dialog';
  dialog.setAttribute('aria-modal', 'true');
  document.body.appendChild(dialog);
  dialog.addEventListener('cancel', event => { event.preventDefault(); dialog.close(); });
  return dialog;
}

function render() {
  ensureDialog();
  const tipKey = TIPS[tipIndex % TIPS.length];
  dialog.innerHTML = `<div class="dialog-head"><h2>${escapeHtml(t('whats_new.title'))}</h2><button type="button" class="icon-button" data-whats-new-close aria-label="${escapeHtml(t('common.close'))}">×</button></div><div class="whats-new-content"><p class="description">${escapeHtml(t('whats_new.intro'))}</p><div class="whats-new-highlights"><article class="detail-card"><h3>${escapeHtml(t('whats_new.say_it_title'))}</h3><p>${escapeHtml(t('whats_new.say_it_body'))}</p></article><article class="detail-card"><h3>${escapeHtml(t('whats_new.record_title'))}</h3><p>${escapeHtml(t('whats_new.record_body'))}</p></article><article class="detail-card"><h3>${escapeHtml(t('whats_new.arcade_title'))}</h3><p>${escapeHtml(t('whats_new.arcade_body'))}</p></article><article class="detail-card"><h3>${escapeHtml(t('whats_new.import_title'))}</h3><p>${escapeHtml(t('whats_new.import_body'))}</p></article></div><section class="whats-new-tip detail-card"><div class="dialog-head"><h3>${escapeHtml(t('whats_new.tip_label'))}</h3><button type="button" class="icon-button" data-whats-new-next>${escapeHtml(t('whats_new.next_tip'))}</button></div><p>${escapeHtml(t(tipKey))}</p></section></div><div class="dialog-actions"><button type="button" class="primary" data-whats-new-close>${escapeHtml(t('whats_new.dismiss'))}</button></div>`;
  dialog.querySelectorAll('[data-whats-new-close]').forEach(button => { button.onclick = () => dialog.close(); });
  dialog.querySelector('[data-whats-new-next]').onclick = () => {
    tipIndex = (tipIndex + 1) % TIPS.length;
    saveTipIndex();
    render();
  };
}

function openWhatsNew() {
  tipIndex = loadTipIndex();
  render();
  if (!dialog.open) dialog.showModal();
}

function initWhatsNew() {
  tipIndex = loadTipIndex();
  $('whatsNewButton')?.addEventListener('click', openWhatsNew);
  document.addEventListener('app:palette-open-whats-new', openWhatsNew);
  document.addEventListener('localechange', () => { if (dialog?.open) render(); });
  window.OpenBoxWhatsNew = { open: openWhatsNew, nextTip: () => { tipIndex = (tipIndex + 1) % TIPS.length; saveTipIndex(); if (dialog?.open) render(); } };
}

export { initWhatsNew, openWhatsNew };
