/* Library navigation — 1.8.0
   Keyboard + gamepad navigation for the library grid and list view.
   Cards are real <button data-game> elements, so focus and Enter already
   work natively; this module moves focus between them (revealing off-window
   cards through library.js virtualization-aware focusGameIndex) and maps
   gamepad buttons through settings.controller_map, mirroring Big Box.
   Dialogs, open context menus, text fields, and Big Box suspend navigation.
*/
import { $, defaultControllerMap } from './util.js';
import { AppState } from './state.js';
import { visibleGameIds, focusGameIndex, gridMetrics, favorite, selectGame } from './library.js';
import { openContextMenu } from './dialogs.js';

const HINT_DISMISS_AFTER_MS = 6000;

function navigationBlocked() {
  if ($('bigBox') && !$('bigBox').hidden) return true;
  if (document.querySelector('dialog[open]')) return true;
  const menu = $('contextMenu');
  if (menu && !menu.hidden) return true;
  const el = document.activeElement;
  if (el && el !== document.body) {
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable) return true;
    if (!$('grid')?.contains(el)) return true;
  }
  if (!AppState.games.length) return true;
  return false;
}

function focusedGameId() {
  const el = document.activeElement?.closest?.('[data-game]');
  if (el) return Number(el.dataset.game);
  return AppState.selectedId;
}

function currentGameIndex() {
  const ids = visibleGameIds();
  if (!ids.length) return -1;
  const id = focusedGameId();
  if (id === null || id === undefined) return -1;
  return ids.indexOf(id);
}

function move(delta, { absolute = false } = {}) {
  const ids = visibleGameIds();
  if (!ids.length) return;
  const current = currentGameIndex();
  const target = absolute ? delta : (current < 0 ? 0 : current + delta);
  focusGameIndex(target);
}

function handleKey(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (navigationBlocked()) return;
  const { cols, page } = gridMetrics();
  const actions = {
    ArrowRight: () => move(1),
    ArrowLeft: () => move(-1),
    ArrowDown: () => move(Math.max(cols, 1)),
    ArrowUp: () => move(-Math.max(cols, 1)),
    PageDown: () => move(Math.max(page, 1)),
    PageUp: () => move(-Math.max(page, 1)),
    Home: () => move(0, { absolute: true }),
    End: () => move(visibleGameIds().length - 1, { absolute: true }),
  };
  const action = actions[event.key];
  if (action) {
    event.preventDefault();
    action();
    return;
  }
  if (event.key === 'f' || event.key === 'F') {
    const id = focusedGameId();
    if (id !== null && id !== undefined) {
      event.preventDefault();
      favorite(id);
    }
    return;
  }
  if (event.key === 'Escape') {
    event.preventDefault();
    selectGame(null);
    document.activeElement?.blur?.();
  }
}

// ── Gamepad ────────────────────────────────────────────────────────────────
let gamepadFrame = 0;
let gamepadPrev = {};

function gamepadActions(pad) {
  const mapping = { ...defaultControllerMap, ...(AppState.appSettings.controller_map || {}) };
  const pressed = action => Boolean(pad.buttons[mapping[action]]?.pressed);
  return {
    left: pad.buttons[14]?.pressed || pad.axes[0] < -0.6,
    right: pad.buttons[15]?.pressed || pad.axes[0] > 0.6,
    up: pad.buttons[12]?.pressed || pad.axes[1] < -0.6,
    down: pad.buttons[13]?.pressed || pad.axes[1] > 0.6,
    play: pressed('play'),
    back: pressed('back'),
    favorite: pressed('favorite'),
    random: pressed('random'),
    pageLeft: pressed('page_left'),
    pageRight: pressed('page_right'),
    menu: pressed('menu'),
  };
}

function pollGamepads() {
  gamepadFrame = 0;
  const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
  const pad = pads[0];
  if (!pad || navigationBlocked()) {
    gamepadPrev = {};
    gamepadFrame = requestAnimationFrame(pollGamepads);
    return;
  }
  const current = gamepadActions(pad);
  const edge = action => current[action] && !gamepadPrev[action];
  const ids = visibleGameIds();
  const { cols, page } = gridMetrics();
  if (edge('left')) move(-1);
  if (edge('right')) move(1);
  if (edge('up')) move(-Math.max(cols, 1));
  if (edge('down')) move(Math.max(cols, 1));
  if (edge('pageLeft')) move(-Math.max(page, 1));
  if (edge('pageRight')) move(Math.max(page, 1));
  if (edge('play') && ids.length) move(currentGameIndex() < 0 ? 0 : currentGameIndex(), { absolute: true });
  if (edge('back')) selectGame(null);
  if (edge('favorite')) {
    const id = focusedGameId();
    if (id !== null && id !== undefined) favorite(id);
  }
  if (edge('random') && ids.length) move(Math.floor(Math.random() * ids.length), { absolute: true });
  if (edge('menu')) {
    const id = focusedGameId();
    const card = id !== null && id !== undefined ? document.querySelector(`[data-game="${id}"]`) : null;
    if (card) {
      const rect = card.getBoundingClientRect();
      openContextMenu({ preventDefault: () => {}, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2, target: card }, id);
    }
  }
  gamepadPrev = current;
  gamepadFrame = requestAnimationFrame(pollGamepads);
}

function startGamepadPoll() {
  if (!gamepadFrame) gamepadFrame = requestAnimationFrame(pollGamepads);
}

function stopGamepadPoll() {
  if (gamepadFrame) cancelAnimationFrame(gamepadFrame);
  gamepadFrame = 0;
  gamepadPrev = {};
}

// ── Hint chip ──────────────────────────────────────────────────────────────
let hintTimer = 0;

function showGamepadHint() {
  const hint = $('gamepadHint');
  if (!hint) return;
  hint.textContent = AppState.appSettings.controller_prompt_hint || 'A Select · B Back · X Favorite · Y Menu · D-pad Navigate';
  hint.hidden = false;
  if (hintTimer) clearTimeout(hintTimer);
  hintTimer = setTimeout(hideGamepadHint, HINT_DISMISS_AFTER_MS);
}

function hideGamepadHint() {
  const hint = $('gamepadHint');
  if (hint) hint.hidden = true;
  if (hintTimer) clearTimeout(hintTimer);
  hintTimer = 0;
}

export function initNavigation() {
  document.addEventListener('keydown', handleKey);
  window.addEventListener('gamepadconnected', () => {
    showGamepadHint();
    startGamepadPoll();
  });
  window.addEventListener('gamepaddisconnected', () => {
    hideGamepadHint();
    if (!(navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean).length : 0)) stopGamepadPoll();
  });
  if (navigator.getGamepads && [...navigator.getGamepads()].filter(Boolean).length) {
    startGamepadPoll();
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopGamepadPoll();
    else if (navigator.getGamepads && [...navigator.getGamepads()].filter(Boolean).length) startGamepadPoll();
  });
}

export { showGamepadHint, hideGamepadHint };
