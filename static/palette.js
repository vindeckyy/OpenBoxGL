/* Command palette (T4): deterministic game search plus real application actions. */
import { $, escapeHtml } from './util.js';
import { AppState, api, notify } from './state.js';
import { launch } from './sessions.js';
import { t } from './i18n.js';
import { searchWithFallback } from './library.js';
import { SHORTCUTS } from './navigation.js';

const ACTIONS = [
  { id: 'settings', label: () => t('tools.settings'), event: 'open-settings' },
  { id: 'time-machine', label: () => t('tools.time_machine'), event: 'open-time-machine' },
  { id: 'bigbox', label: () => t('nav.big_box'), event: 'open-bigbox' },
  { id: 'arcade-room', label: () => t('tools.arcade_room'), event: 'open-arcade-room' },
  { id: 'household', label: () => t('tools.household'), event: 'open-household' },
  { id: 'whats-new', label: () => t('common.whats_new'), event: 'open-whats-new' },
  { id: 'artwork-doctor', label: () => t('tools.artwork_doctor'), event: 'artwork-doctor' },
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

// Recently chosen rows rank first (local usage counts, no telemetry).
const RECENT_KEY = 'openbox-palette-recent';
function recentCounts() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '{}'); } catch { return {}; }
}
function rowKey(row) {
  if (row.type === 'game') return `game:${row.value.game_id || row.value.id}`;
  if (row.type === 'plugin-command') return `plugin-command:${row.value.plugin_id}:${row.value.id}`;
  return `${row.type}:${row.value.id || row.value.event || ''}`;
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

// Plugins 2.0 (F1f): manifest `command` hooks listed under the `>` prefix.
let pluginCommandCache = null;
async function pluginCommandRows(query) {
  try {
    if (!pluginCommandCache) {
      const result = await api('/api/v2/plugins/commands');
      pluginCommandCache = result.commands || [];
    }
    return pluginCommandCache
      .filter(command => !query || String(command.label || '').toLowerCase().includes(query))
      .map(command => ({ type: 'plugin-command', value: command, label: `${command.plugin_id} · ${command.label}` }));
  } catch { return []; }
}
function filteredResults(query) {
  const text = String(query || '').trim().toLowerCase();
  if (text.startsWith('>')) {
    const actionQuery = text.slice(1).trim();
    const builtin = ACTIONS.filter(action => !actionQuery || action.label().toLowerCase().includes(actionQuery)).map(action => ({ type: 'action', value: action, label: action.label() }));
    return pluginCommandRows(actionQuery).then(pluginRows => [...builtin, ...pluginRows]);
  }
  if (text === '?') return shortcutRows();
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
  paletteResults.innerHTML = resultRows.length ? resultRows.map((row, index) => `<button type="button" class="detail-card palette-row${index === selectedIndex ? ' active' : ''}" role="option" aria-selected="${index === selectedIndex}" data-palette-index="${index}">${escapeHtml(row.type === 'action' || row.type === 'plugin-command' ? `> ${row.label}` : row.label)}</button>`).join('') : `<p class="description">${escapeHtml(t('common.no_results'))}</p>`;
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
    paletteResults?.querySelector(`[data-palette-index="${selectedIndex}"]`)?.scrollIntoView({ block: 'nearest' });
  } else if (event.key === 'Enter') {
    event.preventDefault();
    choose(selectedIndex);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    closePalette();
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
  if (row.type === 'help') return;
  if (row.type === 'plugin-command') {
    const command = row.value;
    api('/api/v2/plugins/command',{method:'POST',body:JSON.stringify({plugin_id:command.plugin_id,command:command.id})})
      .then(result => { notify(result.notification || t('plugins.cmd_done')); })
      .catch(error => { notify(error.message); });
    return;
  }
  document.dispatchEvent(new CustomEvent(`app:palette-${row.value.event}`));
}

function openPalette() {
  ensurePalette();
  previousFocus = document.activeElement;
  paletteInput.value = '';
  selectedIndex = 0;
  renderResults();
  if (!paletteDialog.open) paletteDialog.showModal();
  paletteInput.focus();
}

function closePalette() {
  if (paletteDialog?.open) paletteDialog.close();
  previousFocus?.focus?.();
  previousFocus = null;
}

export function initPalette() {
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openPalette();
    }
  });
  document.addEventListener('localechange', () => {
    if (paletteDialog) {
      paletteDialog.remove();
      paletteDialog = null;
      if (document.activeElement?.id === 'commandPaletteInput') openPalette();
    }
  });
}

export { ACTIONS };
