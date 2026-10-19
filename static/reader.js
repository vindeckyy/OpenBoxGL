import { $, defaultControllerMap, prefersReducedMotion, motionMs } from './util.js';
import { AppState, token, api, notify, nativeOpenExternal } from './state.js';
import { matchesKey, keyAction } from './pure.js';
import { registerGamepadSurface, ensureGamepadLoop, syncGamepadLoop } from './gamepad.js';
import { t } from './i18n.js';

const PAGE_MAX = 999;
let turnTimer = 0;
let readerSettingsTimer = 0;
let pendingReaderSettings = {};
let gamepadPrevious = { left: false, right: false, back: false, play: false };

function applyReaderAppearance() {
  const settings = AppState.appSettings || {};
  const dark = (settings.reader_theme || 'light') === 'dark';
  const spread = (settings.reader_spread || 'single') === 'spread';
  $('readerFrame')?.style && ($('readerFrame').style.filter = dark ? 'invert(1) hue-rotate(180deg)' : '');
  $('readerViewport')?.classList.toggle('spread', spread);
  document.querySelectorAll('[data-reader-theme]').forEach(button => {
    button.setAttribute('aria-pressed', String((button.dataset.readerTheme === 'dark') === dark));
  });
  document.querySelectorAll('[data-reader-layout]').forEach(button => {
    button.setAttribute('aria-pressed', String((button.dataset.readerLayout === 'spread') === spread));
  });
}

function persistReaderSetting(patch) {
  AppState.appSettings = { ...(AppState.appSettings || {}), ...patch };
  pendingReaderSettings = { ...pendingReaderSettings, ...patch };
  if (readerSettingsTimer) clearTimeout(readerSettingsTimer);
  readerSettingsTimer = setTimeout(() => {
    const payload = pendingReaderSettings;
    pendingReaderSettings = {};
    readerSettingsTimer = 0;
    api('/api/settings', { method: 'POST', body: JSON.stringify(payload) })
      .then(settings => { AppState.appSettings = settings; })
      .catch(error => notify(error.message));
  }, 300);
}

function openReader(game, index, customUrl) {
  const doc = game.documents?.[index];
  if (!doc) return;
  AppState.readerPage = 1;
  AppState.readerPath = doc.path || '';
  AppState.readerUrl = customUrl || `/api/document?id=${game.id}&index=${index}&token=${encodeURIComponent(token)}`;
  $('readerTitle').textContent = doc.name;
  $('readerOpenExternal').hidden = !AppState.readerPath;
  applyReaderAppearance();
  setReaderPage(1, { animate: false });
  $('readerDialog').showModal();
  ensureGamepadLoop();
}

function setReaderPage(page, { animate = true } = {}) {
  const previous = AppState.readerPage;
  const next = Math.min(PAGE_MAX, Math.max(1, Number(page) || 1));
  if (next === AppState.readerPage && $('readerFrame')?.src) {
    $('readerPageLabel').textContent = `Page ${next}`;
    return;
  }
  AppState.readerPage = next;
  const suffix = AppState.readerUrl.toLowerCase().includes('.pdf') || AppState.readerUrl.includes('/api/document') ? `#page=${AppState.readerPage}` : '';
  const url = `${AppState.readerUrl}${suffix}`;
  const frame = $('readerFrame');
  // Assigning iframe.src pushes a session-history entry per page turn;
  // location.replace keeps Back/Exit honest instead.
  const frameWindow = frame.contentWindow;
  try {
    if (frameWindow) frameWindow.location.replace(url);
    else frame.src = url;
  } catch {
    frame.src = url;
  }
  $('readerPageLabel').textContent = `Page ${AppState.readerPage}`;
  if (animate) animateTurn(next > previous);
}

function animateTurn(forward) {
  const viewport = $('readerViewport');
  if (!viewport || typeof viewport.animate !== 'function') return;
  const reduced = prefersReducedMotion();
  if (reduced) return;
  viewport.classList.remove('reader-turn-next', 'reader-turn-prev');
  void viewport.offsetWidth;
  viewport.classList.add(forward ? 'reader-turn-next' : 'reader-turn-prev');
  clearTimeout(turnTimer);
  turnTimer = setTimeout(() => viewport.classList.remove('reader-turn-next', 'reader-turn-prev'), motionMs('--dur-fast') || 150);
}

function resetReaderFrame() {
  const frame = $('readerFrame');
  if (!frame) return;
  frame.removeAttribute('src');
  try { frame.contentWindow?.location.replace('about:blank'); } catch { /* frame not navigable */ }
  AppState.readerUrl = '';
  AppState.readerPath = '';
  AppState.readerPage = 1;
}

function setReaderSpread(spread) {
  const value = spread === 'spread' ? 'spread' : 'single';
  $('readerViewport').classList.toggle('spread', value === 'spread');
  document.querySelectorAll('[data-reader-layout]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.readerLayout === value));
  });
  persistReaderSetting({ reader_spread: value });
}

function setReaderTheme(theme) {
  const value = theme === 'dark' ? 'dark' : 'light';
  $('readerFrame').style.filter = value === 'dark' ? 'invert(1) hue-rotate(180deg)' : '';
  document.querySelectorAll('[data-reader-theme]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.readerTheme === value));
  });
  persistReaderSetting({ reader_theme: value });
}

function openReaderExternal() {
  const path = AppState.readerPath;
  if (!path) return notify(t('notify.app.reader_no_path'));
  // The bridge accepts allowlisted URI schemes only; file paths use file://.
  const target = /^[a-z][a-z0-9+.-]*:/i.test(path) ? path : `file://${encodeURI(path)}`;
  nativeOpenExternal(target).then(ok => {
    if (!ok) notify(t('notify.app.reader_no_viewer'));
  }).catch(() => notify(t('notify.app.reader_no_viewer')));
}

document.querySelectorAll('[data-reader-layout]').forEach(button => {
  button.onclick = () => setReaderSpread(button.dataset.readerLayout);
});
document.querySelectorAll('[data-reader-theme]').forEach(button => {
  button.onclick = () => setReaderTheme(button.dataset.readerTheme);
});
$('readerPrev').onclick = () => setReaderPage(AppState.readerPage - 1);
$('readerNext').onclick = () => setReaderPage(AppState.readerPage + 1);
$('readerOpenExternal').onclick = openReaderExternal;

document.addEventListener('keydown', event => {
  if (!$('readerDialog')?.open) return;
  const target = event.target;
  if (target instanceof Element && target.closest('input,textarea,select,[contenteditable="true"]')) return;
  if (matchesKey(event, keyAction(AppState.appSettings, 'reader_prev')) || event.key === 'Home') {
    event.preventDefault();
    setReaderPage(event.key === 'Home' ? 1 : AppState.readerPage - 1);
    return;
  }
  if (matchesKey(event, keyAction(AppState.appSettings, 'reader_next')) || event.key === 'End') {
    event.preventDefault();
    setReaderPage(event.key === 'End' ? PAGE_MAX : AppState.readerPage + 1);
    return;
  }
  if (matchesKey(event, keyAction(AppState.appSettings, 'back'))) {
    event.preventDefault();
    $('readerDialog').close();
  }
});

function pollReaderGamepads() {
  const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
  let left = false, right = false, back = false, play = false;
  for (const pad of pads) {
    const mapping = { ...defaultControllerMap, ...(AppState.appSettings.controller_map || {}) };
    if (pad.buttons[14]?.pressed || (pad.axes[0] ?? 0) < -0.6) left = true;
    if (pad.buttons[15]?.pressed || (pad.axes[0] ?? 0) > 0.6) right = true;
    if (pad.buttons[mapping.back]?.pressed) back = true;
    if (pad.buttons[mapping.play]?.pressed) play = true;
  }
  if (left && !gamepadPrevious.left) setReaderPage(AppState.readerPage - 1);
  if (right && !gamepadPrevious.right) setReaderPage(AppState.readerPage + 1);
  if (back && !gamepadPrevious.back) $('readerDialog').close();
  if (play && !gamepadPrevious.play) setReaderSpread($('readerViewport').classList.contains('spread') ? 'single' : 'spread');
  gamepadPrevious = { left, right, back, play };
}

registerGamepadSurface({
  priority: 20,
  isActive: () => Boolean($('readerDialog')?.open),
  tick: pollReaderGamepads,
});

// Keep the pad loop awake when the reader is the only surface that needs it.
syncGamepadLoop();

export { openReader, setReaderPage, resetReaderFrame, setReaderSpread, setReaderTheme };
