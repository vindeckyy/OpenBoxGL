/* Command palette (T4): deterministic game search plus real application actions. */
import { $, escapeHtml, prefersReducedMotion } from './util.js';
import { AppState, api, notify, notifyError } from './state.js';
import { launch } from './sessions.js';
import { t, onLocaleChange } from './i18n.js';
import { searchWithFallback, favorite } from './library.js';
import { captureMomentInteractive } from './moments.js';
import { openDialog, closeDialog } from './dialogs.js';
import { SHORTCUTS } from './navigation.js';

const ACTIONS = [
  { id: 'settings', label: () => t('tools.settings'), event: 'open-settings' },
  { id: 'time-machine', label: () => t('tools.time_machine'), event: 'open-time-machine' },
  { id: 'bigbox', label: () => t('nav.big_box'), event: 'open-bigbox' },
  { id: 'arcade-room', label: () => t('tools.arcade_room'), event: 'open-arcade-room' },
  { id: 'household', label: () => t('tools.household'), event: 'open-household' },
  { id: 'whats-new', label: () => t('common.whats_new'), event: 'open-whats-new' },
  { id: 'artwork-doctor', label: () => t('tools.artwork_doctor'), event: 'artwork-doctor' },
  { id: 'launch-doctor', label: () => t('tools.launch_doctor'), event: 'launch-doctor' },
  { id: 'surprise', label: () => t('library.surprise_me'), event: 'surprise' },
  { id: 'search', label: () => t('common.search'), event: 'focus-search' },
];

let paletteDialog = null;
let paletteInput = null;
let paletteResults = null;
let previousFocus = null;
let selectedIndex = 0;
let resultRows = [];
let resultsRequest = 0;
let pluginCommands = [];
let shortcutsDialog = null;

// Recently chosen rows rank first (local usage counts, no telemetry).
const RECENT_KEY = 'openbox-palette-recent';
function recentCounts() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '{}'); } catch { return {}; }
}
function rowKey(row) {
  return row.type === 'game' ? `game:${row.value.game_id || row.value.id}` : `${row.type}:${row.value.id || row.value.event || ''}`;
}
function rankRecent(rows) {
  const counts = recentCounts();
  return rows.map((row, index) => ({ row, index }))
    .sort((a, b) => (counts[rowKey(b.row)] || 0) - (counts[rowKey(a.row)] || 0) || a.index - b.index)
    .map(item => item.row);
}

function ensurePalette() {
  if (paletteDialog) return paletteDialog;
  paletteDialog = document.createElement('dialog');
  paletteDialog.id = 'commandPaletteDialog';
  paletteDialog.className = 'command-palette';
  paletteDialog.setAttribute('aria-modal', 'true');
  paletteDialog.innerHTML = `<div class="dialog-head"><h2>${escapeHtml(t('common.search'))}</h2><button type="button" class="icon-button" data-palette-close aria-label="${escapeHtml(t('common.cancel'))}">×</button></div><div class="form-grid"><label class="field wide"><span>${escapeHtml(t('common.search'))}</span><input type="search" id="commandPaletteInput" autocomplete="off" spellcheck="false"></label><div class="wide metadata-results" id="commandPaletteResults" role="listbox"></div></div>`;
  document.body.appendChild(paletteDialog);
  paletteInput = paletteDialog.querySelector('#commandPaletteInput');
  paletteResults = paletteDialog.querySelector('#commandPaletteResults');
  paletteDialog.querySelector('[data-palette-close]').onclick = closePalette;
  paletteDialog.addEventListener('cancel', event => { event.preventDefault(); closePalette(); });
  paletteInput.addEventListener('input', renderResults);
  paletteInput.addEventListener('keydown', handleKeydown);
  return paletteDialog;
}

function gameLabel(game) {
  const meta = [game.platform, game.progress].filter(Boolean).join(' · ');
  return meta ? `${game.name} · ${meta}` : String(game.name || '');
}

function shortcutRows() {
  return SHORTCUTS.map(shortcut => ({
    type: 'help',
    value: shortcut,
    label: `${shortcut.key} — ${t(shortcut.labelKey)}`,
  }));
}

// P6: `?` searches a generated help index instead of exact-matching only `?`.
// The index is built from the same SHORTCUTS table navigation.js consumes, so
// the palette can never show a shortcut the app does not implement, plus the
// runnable actions from `>`. Selecting a shortcut runs its action when one
// exists (fixes the old dead end where choosing a help row did nothing).
function helpRows(query) {
  const text = String(query || '').trim().toLowerCase();
  const rows = [
    { type: 'shortcuts', value: {}, label: t('shortcuts.show_shortcuts') },
    ...shortcutRows(),
    ...ACTIONS.map(action => ({ type: 'action', value: action, label: `> ${action.label()}` })),
  ];
  return text ? rows.filter(row => row.label.toLowerCase().includes(text)) : rows;
}

// Grouped cheat sheet generated from navigation.js SHORTCUTS, opened with `?`
// outside a text field or from the palette help index. It reuses the shared
// dialog host/focus restore in dialogs.js instead of hand-rolling showModal.
function ensureShortcutsDialog() {
  if (shortcutsDialog) return shortcutsDialog;
  shortcutsDialog = document.createElement('dialog');
  shortcutsDialog.id = 'shortcutsDialog';
  shortcutsDialog.className = 'shortcuts-dialog';
  shortcutsDialog.setAttribute('aria-modal', 'true');
  shortcutsDialog.innerHTML = `<div class="dialog-head"><h2>${escapeHtml(t('shortcuts.title'))}</h2><button type="button" class="icon-button" data-shortcuts-close aria-label="${escapeHtml(t('common.close'))}">×</button></div><div class="form-grid shortcuts-groups" id="shortcutsBody"></div>`;
  document.body.appendChild(shortcutsDialog);
  shortcutsDialog.querySelector('[data-shortcuts-close]').onclick = () => closeDialog(shortcutsDialog);
  shortcutsDialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(shortcutsDialog); });
  return shortcutsDialog;
}

function renderShortcutsDialog() {
  const body = shortcutsDialog?.querySelector('#shortcutsBody');
  if (!body) return;
  const groups = new Map();
  for (const shortcut of SHORTCUTS) {
    const group = shortcut.group || 'shortcuts.group_global';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(shortcut);
  }
  body.innerHTML = [...groups.entries()].map(([group, rows]) => `
    <section class="shortcut-group wide">
      <h3>${escapeHtml(t(group))}</h3>
      <dl class="shortcut-list">${rows.map(row => `<div class="shortcut-row"><dt><kbd>${escapeHtml(row.key)}</kbd></dt><dd>${escapeHtml(t(row.labelKey))}</dd></div>`).join('')}</dl>
    </section>`).join('');
}

export function openShortcutCheatSheet() {
  ensureShortcutsDialog();
  renderShortcutsDialog();
  if (!shortcutsDialog.open) openDialog(shortcutsDialog);
}

const SHORTCUT_RUNNERS = {
  'shortcuts.command_palette': () => { openPalette(); },
  'shortcuts.capture_moment': () => {
    const session = AppState.runningGames[0];
    const game = AppState.games.find(item => item.id === session?.game_id || String(item.game_id) === String(session?.game_id)) ||
      AppState.games.find(item => item.id === AppState.selectedId);
    if (game) captureMomentInteractive(game, { trigger: 'palette' }).catch(() => {});
    else notify('info', t('palette.select_game_hint'));
  },
  'shortcuts.favorite_focused': () => {
    const id = AppState.selectedId;
    if (id !== null && id !== undefined) favorite(id);
  },
  'shortcuts.close_selection': () => {},
};

function filteredResults(query) {
  const text = String(query || '').trim().toLowerCase();
  if (text.startsWith('>')) {
    const actionQuery = text.slice(1).trim();
    const actions = ACTIONS.map(action => ({ type: 'action', value: action, label: action.label() }));
    const plugins = pluginCommands.map(command => ({
      type: 'plugin',
      value: command,
      label: `${command.label} · ${command.plugin_id}`,
    }));
    return [...actions, ...plugins].filter(row => !actionQuery || row.label.toLowerCase().includes(actionQuery));
  }
  if (text.startsWith('?')) return helpRows(text.slice(1));
  if (!text) return AppState.games.slice(0, 12).map(game => ({ type: 'game', value: game, label: gameLabel(game) }));
  return searchWithFallback(text, AppState.games).then(matches =>
    matches.slice(0, 20).map(game => ({ type: 'game', value: game, label: gameLabel(game) }))
  );
}

async function renderResults() {
  if (!paletteResults) return;
  const request = ++resultsRequest;
  const rows = filteredResults(paletteInput.value);
  resultRows = rankRecent(rows && typeof rows.then === 'function' ? await rows : rows);
  if (request !== resultsRequest || !paletteResults) return;
  selectedIndex = Math.max(0, Math.min(selectedIndex, resultRows.length - 1));
  paletteResults.innerHTML = resultRows.length ? resultRows.map((row, index) => `<button type="button" class="detail-card palette-row${index === selectedIndex ? ' active' : ''}" role="option" aria-selected="${index === selectedIndex}" data-palette-index="${index}">${escapeHtml(row.type === 'action' ? `> ${row.label}` : row.label)}</button>`).join('') : `<p class="description">${escapeHtml(t('common.no_results'))}</p>`;
  paletteResults.querySelectorAll('[data-palette-index]').forEach(button => {
    button.onclick = () => choose(Number(button.dataset.paletteIndex));
  });
}

function handleKeydown(event) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const delta = event.key === 'ArrowDown' ? 1 : -1;
    selectedIndex = resultRows.length ? (selectedIndex + delta + resultRows.length) % resultRows.length : 0;
    renderResults();
    paletteResults?.querySelector(`[data-palette-index="${selectedIndex}"]`)?.scrollIntoView({
      block: 'nearest',
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    });
  } else if (event.key === 'Enter') {
    event.preventDefault();
    choose(selectedIndex);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    closePalette();
  }
}

async function loadPluginCommands() {
  try {
    const payload = await api('/api/v2/plugins/commands');
    pluginCommands = Array.isArray(payload?.commands) ? payload.commands : [];
  } catch {
    pluginCommands = [];
  }
}

function choose(index) {
  const row = resultRows[index];
  if (!row) return;
  try {
    const counts = recentCounts();
    const key = rowKey(row);
    counts[key] = (counts[key] || 0) + 1;
    localStorage.setItem(RECENT_KEY, JSON.stringify(counts));
  } catch {}
  closePalette();
  if (row.type === 'game') {
    AppState.selectedId = row.value.id;
    document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: row.value.game_id || row.value.id } }));
    launch(row.value).catch(() => {});
    return;
  }
  if (row.type === 'help') {
    SHORTCUT_RUNNERS[row.value.labelKey]?.();
    return;
  }
  if (row.type === 'shortcuts') {
    openShortcutCheatSheet();
    return;
  }
  if (row.type === 'plugin') {
    api('/api/v2/plugins/command', {
      method: 'POST',
      body: JSON.stringify({ plugin_id: row.value.plugin_id, command: row.value.id }),
    }).then(result => {
      const note = result?.notification;
      if (note?.message) notify(note.level || 'info', note.message);
      else notify('success', t('palette.plugin_command_done', { label: row.value.label }));
    }).catch(error => notifyError(error));
    return;
  }
  document.dispatchEvent(new CustomEvent(`app:palette-${row.value.event}`));
}

async function openPalette() {
  ensurePalette();
  previousFocus = document.activeElement;
  paletteInput.value = '';
  selectedIndex = 0;
  await loadPluginCommands();
  renderResults();
  if (!paletteDialog.open) openDialog(paletteDialog, previousFocus || undefined);
  paletteInput.focus();
}

function closePalette() {
  if (paletteDialog?.open) closeDialog(paletteDialog);
  if (previousFocus?.isConnected) previousFocus.focus?.();
  previousFocus = null;
}

export function initPalette() {
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openPalette();
      return;
    }
    // `?` outside a text field and with no dialog open opens the cheat sheet;
    // inside the palette it stays part of the searchable help index.
    if (event.key !== '?' || event.ctrlKey || event.metaKey || event.altKey) return;
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT' || active.isContentEditable)) return;
    if (document.querySelector('dialog[open]')) return;
    event.preventDefault();
    openShortcutCheatSheet();
  });
  // P8: rebuild the dialog markup after a language change; the old listener
  // only recovered focus, leaving the header in the previous language.
  onLocaleChange(() => {
    if (shortcutsDialog) {
      const wasOpen = shortcutsDialog.open;
      shortcutsDialog.remove();
      shortcutsDialog = null;
      if (wasOpen) openShortcutCheatSheet();
    }
    if (!paletteDialog) return;
    const wasOpen = paletteDialog.open;
    paletteDialog.remove();
    paletteDialog = null;
    if (wasOpen) openPalette();
  });
}

export { ACTIONS };
