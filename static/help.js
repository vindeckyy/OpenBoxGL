import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { openDialog, closeDialog } from './dialogs.js';

const HELP_TABS = [
  { id: 'start', tabId: 'helpTabStart', panelId: 'helpPanelStart', labelKey: 'help.tab_start' },
  { id: 'glossary', tabId: 'helpTabGlossary', panelId: 'helpPanelGlossary', labelKey: 'help.tab_glossary' },
  { id: 'shortcuts', tabId: 'helpTabShortcuts', panelId: 'helpPanelShortcuts', labelKey: 'help.tab_shortcuts' },
];

const HELP_START = [
  { action: 'add', titleKey: 'help.start_add', bodyKey: 'help.start_add_body' },
  { action: 'emulators', titleKey: 'help.start_emulators', bodyKey: 'help.start_emulators_body' },
  { action: 'artwork', titleKey: 'help.start_artwork', bodyKey: 'help.start_artwork_body' },
  { action: 'play', titleKey: 'help.start_play', bodyKey: 'help.start_play_body' },
];

const GLOSSARY_TERMS = ['shelf', 'emulator', 'rom', 'bios', 'metadata', 'artwork', 'profile', 'playlist', 'collection', 'bigbox', 'resume'];

// Keep this table in step with static/navigation.js when a shortcut changes.
const SHORTCUTS = Object.freeze([
  { key: 'Ctrl/Cmd+K', labelKey: 'shortcuts.command_palette' },
  { key: '/', labelKey: 'help.shortcut_search' },
  { key: 'Enter', labelKey: 'help.shortcut_play' },
  { key: '↑ ↓ ← →', labelKey: 'help.shortcut_navigate' },
  { key: 'F', labelKey: 'shortcuts.favorite_focused' },
  { key: 'Shift+M', labelKey: 'shortcuts.capture_moment' },
  { key: 'Esc', labelKey: 'shortcuts.close_selection' },
  { key: 'F1 / ?', labelKey: 'help.shortcut_help' },
]);

let activeHelpTab = 'start';
let helpInitialized = false;

function ensureHelp() {
  if (!helpInitialized) {
    $('closeHelp').onclick = closeHelp;
    $('helpTabs').addEventListener('keydown', handleTabKeydown);
    helpInitialized = true;
  }
  return $('helpDialog');
}

function handleTabKeydown(event) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const current = HELP_TABS.findIndex(tab => tab.id === activeHelpTab);
  const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
  let next = current;
  if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = HELP_TABS.length - 1;
  else next = (Math.max(0, current) + delta + HELP_TABS.length) % HELP_TABS.length;
  event.preventDefault();
  activeHelpTab = HELP_TABS[next].id;
  renderHelpTabs();
  renderHelpBody();
  $(HELP_TABS[next].tabId)?.focus();
}

export function openHelp(tab = 'start') {
  if (!HELP_TABS.some(item => item.id === tab)) tab = 'start';
  activeHelpTab = tab;
  ensureHelp();
  renderHelpTabs();
  renderHelpBody();
  openDialog($('helpDialog'));
  $(HELP_TABS.find(item => item.id === tab)?.tabId)?.focus();
}

export function closeHelp() {
  closeDialog($('helpDialog'));
}

function renderHelpTabs() {
  const tabs = $('helpTabs');
  if (!tabs) return;
  tabs.innerHTML = HELP_TABS.map(tab => {
    const active = tab.id === activeHelpTab;
    return `<button type="button" class="dialog-tab${active ? ' active' : ''}" id="${tab.tabId}" role="tab" aria-selected="${active}" aria-controls="${tab.panelId}" data-help-tab="${tab.id}">${escapeHtml(t(tab.labelKey))}</button>`;
  }).join('');
  tabs.querySelectorAll('[data-help-tab]').forEach(button => {
    button.onclick = () => {
      activeHelpTab = button.dataset.helpTab;
      renderHelpTabs();
      renderHelpBody();
      $(button.id)?.focus();
    };
  });
}

function renderHelpBody() {
  const body = $('helpBody');
  const tab = HELP_TABS.find(item => item.id === activeHelpTab) || HELP_TABS[0];
  if (!body || !tab) return;
  if (tab.id === 'glossary') {
    body.innerHTML = `<section role="tabpanel" id="${tab.panelId}" aria-labelledby="${tab.tabId}">
      <h3>${escapeHtml(t('help.glossary_title'))}</h3>
      <dl class="help-glossary">${GLOSSARY_TERMS.map(term => `<div><dt>${escapeHtml(t(`help.g_${term}_term`))}</dt><dd>${escapeHtml(t(`help.g_${term}_body`))}</dd></div>`).join('')}</dl>
    </section>`;
    return;
  }
  if (tab.id === 'shortcuts') {
    body.innerHTML = `<section role="tabpanel" id="${tab.panelId}" aria-labelledby="${tab.tabId}">
      <h3>${escapeHtml(t('help.shortcuts_title'))}</h3>
      <table class="help-shortcuts"><thead><tr><th>${escapeHtml(t('help.col_key'))}</th><th>${escapeHtml(t('help.col_action'))}</th></tr></thead>
      <tbody>${SHORTCUTS.map(shortcut => `<tr><td><kbd>${escapeHtml(shortcut.key)}</kbd></td><td>${escapeHtml(t(shortcut.labelKey))}</td></tr>`).join('')}</tbody></table>
    </section>`;
    return;
  }
  body.innerHTML = `<section role="tabpanel" id="${tab.panelId}" aria-labelledby="${tab.tabId}">
    <p class="description">${escapeHtml(t('help.intro'))}</p>
    <div class="help-grid">${HELP_START.map(card => `<button type="button" class="help-topic" data-help-action="${card.action}"><strong>${escapeHtml(t(card.titleKey))}</strong><span>${escapeHtml(t(card.bodyKey))}</span></button>`).join('')}</div>
    <div class="extras"><button type="button" class="primary" id="helpOpenSetup">${escapeHtml(t('help.reopen_setup'))}</button>
    <button type="button" class="icon-button" id="helpOpenHealth">${escapeHtml(t('help.checkup'))}</button>
    <button type="button" class="icon-button" id="helpReportIssue">${escapeHtml(t('help.report_issue'))}</button></div>
  </section>`;
  bindHelpActions();
}

function bindHelpActions() {
  document.querySelectorAll('[data-help-action]').forEach(button => {
    button.onclick = () => runHelpAction(button.dataset.helpAction);
  });
  const setup = $('helpOpenSetup');
  if (setup) setup.onclick = () => runHelpAction('setup');
  const health = $('helpOpenHealth');
  if (health) health.onclick = () => runHelpAction('health');
  const issue = $('helpReportIssue');
  if (issue) issue.onclick = () => runHelpAction('issue');
}

function runHelpAction(action) {
  closeHelp();
  if (action === 'add') {
    const target = $('emptyScanFolder') || $('importButton');
    target?.click();
  } else if (action === 'emulators') $('emulatorsButton')?.click();
  else if (action === 'artwork') $('metadataButton')?.click();
  else if (action === 'play') $('libraryButton')?.click();
  else if (action === 'setup') $('setupLibraryButton')?.click();
  else if (action === 'health') $('healthButton')?.click();
  else if (action === 'issue') $('reportIssueButton')?.click();
}

document.addEventListener('app:palette-open-help', () => openHelp());
document.addEventListener('localechange', () => {
  if ($('helpDialog')?.open) {
    renderHelpTabs();
    renderHelpBody();
  }
});
