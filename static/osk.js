/* Big Box on-screen keyboard (F5c, v1): d-pad-navigable QWERTY + symbols grid.
 *
 * Big Box-native only: opens when #bigBoxHybridSearch takes focus and emits
 * into it by dispatching 'input' events, so the existing filter handler runs
 * untouched. Registers a gamepad surface at priority 15 — below the pause
 * overlay (10), above the arcade room (20) — via the unified loop in
 * gamepad.js. DOM focus stays in the input (mousedown is swallowed on keys),
 * so physical keyboards keep working and the gamepad cursor is visual only.
 */
import { $, defaultControllerMap } from './util.js';
import { AppState } from './state.js';
import { t } from './i18n.js';
import { registerGamepadSurface, ensureGamepadLoop } from './gamepad.js';

const LETTER_ROWS = [
  ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
  ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
  ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
  ['z', 'x', 'c', 'v', 'b', 'n', 'm'],
];
const SYMBOL_ROWS = [
  ['!', '@', '#', '$', '%', '^', '&', '*', '(', ')'],
  ['-', '_', '=', '+', '[', ']', '{', '}', ';', ':'],
  ['\'', '"', ',', '.', '/', '?', '\\', '|', '~'],
];

let oskPage = 'letters';
let oskRow = 0;
let oskCol = 0;
let oskTarget = null;
let oskPrev = {};
let oskButtons = [];

function oskRows() {
  const chars = oskPage === 'symbols' ? SYMBOL_ROWS : LETTER_ROWS;
  const rows = chars.map(row => row.map(char => ({ label: char, char })));
  rows.push([
    { label: oskPage === 'symbols' ? t('bigbox.osk_letters') : t('bigbox.osk_symbols'), action: 'toggle', wide: true },
    { label: t('bigbox.osk_space'), action: 'space', wide: true },
    { label: '⌫', action: 'backspace' },
    { label: '✓', action: 'close' },
  ]);
  return rows;
}

function highlightOsk() {
  oskButtons.forEach((row, r) => row.forEach((button, c) => {
    button.classList.toggle('osk-selected', r === oskRow && c === oskCol);
  }));
}

function renderOsk() {
  const mount = $('bigBoxOsk');
  if (!mount) return;
  mount.innerHTML = '';
  const title = document.createElement('div');
  title.className = 'osk-title';
  title.textContent = t('bigbox.osk_title');
  mount.appendChild(title);
  const grid = document.createElement('div');
  grid.className = 'osk-grid';
  oskButtons = [];
  oskRows().forEach((row, r) => {
    const rowEl = document.createElement('div');
    rowEl.className = 'osk-row';
    const buttonRow = [];
    row.forEach((key, c) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `osk-key${key.wide ? ' osk-wide' : ''}`;
      button.textContent = key.label;
      // Keep DOM focus in the input: the gamepad cursor is visual only.
      button.addEventListener('mousedown', event => event.preventDefault());
      button.addEventListener('click', () => activateOskKey(r, c));
      rowEl.appendChild(button);
      buttonRow.push(button);
    });
    grid.appendChild(rowEl);
    oskButtons.push(buttonRow);
  });
  mount.appendChild(grid);
  highlightOsk();
}

function emitOskText(text) {
  const input = oskTarget;
  if (!input) return;
  const value = input.value || '';
  const start = input.selectionStart ?? value.length;
  const end = input.selectionEnd ?? value.length;
  input.value = value.slice(0, start) + text + value.slice(end);
  const caret = start + text.length;
  try { input.setSelectionRange(caret, caret); } catch { /* non-text input */ }
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
}

function backspaceOsk() {
  const input = oskTarget;
  if (!input) return;
  const value = input.value || '';
  const start = input.selectionStart ?? value.length;
  const end = input.selectionEnd ?? value.length;
  let next = start;
  if (start !== end) {
    input.value = value.slice(0, start) + value.slice(end);
  } else if (start > 0) {
    input.value = value.slice(0, start - 1) + value.slice(end);
    next = start - 1;
  }
  try { input.setSelectionRange(next, next); } catch { /* non-text input */ }
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
}

function activateOskKey(row, col) {
  const key = oskRows()[row]?.[col];
  if (!key) return;
  if (key.char) emitOskText(key.char);
  else if (key.action === 'space') emitOskText(' ');
  else if (key.action === 'backspace') backspaceOsk();
  else if (key.action === 'toggle') {
    oskPage = oskPage === 'symbols' ? 'letters' : 'symbols';
    oskRow = 0;
    oskCol = 0;
    renderOsk();
  } else if (key.action === 'close') closeOsk(true);
}

function moveOsk(rowDelta, colDelta) {
  const rows = oskRows();
  oskRow = Math.max(0, Math.min(rows.length - 1, oskRow + rowDelta));
  oskCol = Math.max(0, Math.min(rows[oskRow].length - 1, oskCol + colDelta));
  highlightOsk();
}

function oskPad() {
  const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
  return pads[0];
}

function oskPadState(pad) {
  const mapping = { ...defaultControllerMap, ...(AppState.appSettings.controller_map || {}) };
  const pressed = action => Boolean(pad.buttons[mapping[action]]?.pressed);
  return {
    left: Boolean(pad.buttons[14]?.pressed) || pad.axes[0] < -0.6,
    right: Boolean(pad.buttons[15]?.pressed) || pad.axes[0] > 0.6,
    up: Boolean(pad.buttons[12]?.pressed) || pad.axes[1] < -0.6,
    down: Boolean(pad.buttons[13]?.pressed) || pad.axes[1] > 0.6,
    play: pressed('play'),
    back: pressed('back'),
  };
}

function pollOsk() {
  if (document.hidden || !document.hasFocus()) return;
  const pad = oskPad();
  if (!pad) return;
  const current = oskPadState(pad);
  const edge = action => current[action] && !oskPrev[action];
  if (edge('up')) moveOsk(-1, 0);
  if (edge('down')) moveOsk(1, 0);
  if (edge('left')) moveOsk(0, -1);
  if (edge('right')) moveOsk(0, 1);
  if (edge('play')) activateOskKey(oskRow, oskCol);
  if (edge('back')) closeOsk(true);
  if (Object.keys(current).some(edge)) AppState.bigBoxLastInput = performance.now();
  oskPrev = current;
}

function oskVisible() {
  const mount = $('bigBoxOsk');
  const box = $('bigBox');
  return Boolean(mount && box && !mount.hidden && !box.hidden);
}

/** Open the keyboard feeding `target` (defaults to the Big Box search). */
export function openOsk(target) {
  oskTarget = target || $('bigBoxHybridSearch');
  if (!oskTarget) return;
  oskPage = 'letters';
  oskRow = 0;
  oskCol = 0;
  oskPrev = {};
  renderOsk();
  const mount = $('bigBoxOsk');
  if (!mount) return;
  mount.hidden = false;
  ensureGamepadLoop();
}

/** Close the keyboard; when `toBigBox` is true, return focus to Big Box itself. */
export function closeOsk(toBigBox) {
  const mount = $('bigBoxOsk');
  if (mount) mount.hidden = true;
  if (toBigBox) {
    try { oskTarget?.blur(); } catch { /* already detached */ }
    $('bigBox')?.focus();
  }
  oskTarget = null;
  oskPrev = {};
}

export function oskIsOpen() {
  return oskVisible();
}

registerGamepadSurface({ priority: 15, isActive: oskVisible, tick: pollOsk });

if (typeof document !== 'undefined') {
  const search = $('bigBoxHybridSearch');
  search?.addEventListener('focus', () => openOsk(search));
  search?.addEventListener('blur', event => {
    if (!event.relatedTarget?.closest?.('#bigBoxOsk')) closeOsk(false);
  });
}
