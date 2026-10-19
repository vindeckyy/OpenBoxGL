/* CSS-only 3D cover models. No WebGL, no external assets: the front cover is
   the game's existing box art and the spine/back fall back to it. */
import { $, escapeHtml } from './util.js';
import { clampAngle } from './pure.js';
import { registerGamepadSurface, ensureGamepadLoop, syncGamepadLoop } from './gamepad.js';

export { clampAngle };
import { AppState, media, coverSource } from './state.js';

export const COVER_MODELS = ['box', 'jewel', 'long_jewel', 'double_jewel', 'cassette', 'none'];

export function coverModelFor(game) {
  const settings = AppState.appSettings || {};
  const platform = game?.platform || '';
  const choices = [game?.cover_model, settings.cover_model_by_platform?.[platform], settings.cover_model];
  return choices.find(value => COVER_MODELS.includes(value)) || 'box';
}

export function modelStyleFor(game) {
  const settings = AppState.appSettings || {};
  const platform = game?.platform || '';
  return {
    model: coverModelFor(game),
    back: settings.model_back_by_platform?.[platform] || 'angled',
    caseStyle: settings.model_case_by_platform?.[platform] || 'clear',
    rotationLock: settings.model_rotation_lock_by_platform?.[platform] || settings.model_rotation_lock || 'free',
  };
}

function imageTag(game, kind, className) {
  const has = kind === 'cover' ? game.has_cover : kind === 'box_back' ? game.has_box_back : game.has_box_spine;
  if (!has) return `<span class="${className} model-fallback">${escapeHtml(game.name || '')}</span>`;
  return `<img class="${className}" src="${kind === 'cover' ? coverSource(game) : media(game, kind)}" alt="" loading="lazy" decoding="async">`;
}

export function renderCoverModel(game, options = {}) {
  const style = modelStyleFor(game);
  const chosen = options.model || style.model;
  const rotationLock = options.rotationLock || style.rotationLock;
  const angle = options.angle || 0;
  const angleX = options.angleX || 0;
  const fullscreen = Boolean(options.fullscreen);
  const tools = options.tools !== false;
  if (chosen === 'none') return `<div class="model-flat">${imageTag(game, 'cover', 'model-flat-image')}</div>`;
  const rotated = clampAngle(angle, rotationLock, 'y');
  const tilted = clampAngle(angleX, rotationLock, 'x');
  const backStyle = (options.back || style.back) === 'straight' ? 'straight' : 'angled';
  const caseKind = (options.caseStyle || style.caseStyle) === 'solid' ? 'solid' : 'clear';
  const modelTools = tools
    ? '<div class="model-tools"><button type="button" data-model-rotate="15" aria-label="Rotate right">↻</button><button type="button" data-model-flip aria-label="Flip model">⇄</button><button type="button" data-model-fullscreen aria-label="Full screen model">⛶</button></div>'
    : '';
  return `<div class="model-3d" data-model="${escapeHtml(chosen)}" data-rotation-lock="${escapeHtml(rotationLock)}" data-back="${backStyle}" data-case="${caseKind}" style="--model-angle:${rotated}deg;--model-angle-x:${tilted}deg"${fullscreen ? ' data-model-fullscreen="1"' : ''}>
    <div class="model-rotator">
      <div class="model-face model-back">${imageTag(game, 'box_back', 'model-cover-image')}</div>
      <div class="model-face model-spine model-spine-first">${imageTag(game, 'box_spine', 'model-spine-image')}</div>
      <div class="model-face model-front">${imageTag(game, 'cover', 'model-cover-image')}<span class="model-jcard">${escapeHtml(game.name || '')}</span></div>
      ${chosen === 'double_jewel' ? `<div class="model-face model-spine model-spine-alt">${imageTag(game, 'box_spine', 'model-spine-image')}</div>` : ''}
    </div>
    ${modelTools}
  </div>`;
}

export function bindCoverModel(root, { rotationLock = '' } = {}) {
  if (!root) return;
  const model = root.querySelector('.model-3d');
  if (!model) return;
  rotationLock = rotationLock || model.dataset.rotationLock || 'free';
  let drag = null;
  const readAngle = () => Number.parseFloat(model.style.getPropertyValue('--model-angle')) || 0;
  const readTilt = () => Number.parseFloat(model.style.getPropertyValue('--model-angle-x')) || 0;
  const apply = (angle = readAngle(), angleX = readTilt()) => {
    model.style.setProperty('--model-angle', `${angle}deg`);
    model.style.setProperty('--model-angle-x', `${angleX}deg`);
  };
  root.querySelectorAll('[data-model-rotate]').forEach(button => {
    button.onclick = event => {
      event.preventDefault();
      event.stopPropagation();
      apply(readAngle() + (Number(button.dataset.modelRotate) || 15), readTilt());
    };
  });
  root.querySelectorAll('[data-model-flip]').forEach(button => {
    button.onclick = event => {
      event.preventDefault();
      event.stopPropagation();
      apply(readAngle() + 180, readTilt());
    };
  });
  root.querySelectorAll('[data-model-fullscreen]').forEach(button => {
    button.onclick = event => {
      event.preventDefault();
      event.stopPropagation();
      if (document.fullscreenElement === model) document.exitFullscreen?.();
      else { ensureGamepadLoop(); model.requestFullscreen?.().catch(() => {}); }
    };
  });
  model.addEventListener('pointerdown', event => {
    if (event.target.closest('button')) return;
    drag = { x: event.clientX, y: event.clientY, startAngle: readAngle(), startAngleX: readTilt() };
    model.setPointerCapture?.(event.pointerId);
  });
  model.addEventListener('pointermove', event => {
    if (!drag) return;
    const angle = rotationLock !== 'vertical'
      ? clampAngle(drag.startAngle + (event.clientX - drag.x) * 0.4, 'free', 'y')
      : readAngle();
    const angleX = rotationLock !== 'horizontal'
      ? clampAngle(drag.startAngleX - (event.clientY - drag.y) * 0.4, 'free', 'x')
      : readTilt();
    apply(angle, angleX);
  });
  const release = () => { drag = null; };
  model.addEventListener('pointerup', release);
  model.addEventListener('pointercancel', release);
  model.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    apply(readAngle() + (event.key === 'ArrowRight' ? 15 : -15), readTilt());
  });
  model.tabIndex = 0;
}

export function mountCoverModel(selector, game, options = {}) {
  const host = typeof selector === 'string' ? $(selector) : selector;
  if (!host) return;
  host.innerHTML = renderCoverModel(game, options);
  bindCoverModel(host, options);
}

// Fullscreen 3D models keep the gamepad usable without stealing Big Box or
// library navigation: the surface is active only while the browser reports the
// model itself as the fullscreen element. Left/right rotate in the same 15°
// steps as the arrow keys; the bounded loop wakes on fullscreenentry.
let fullscreenPadPrev = {};
function fullscreenModelElement() {
  const element = typeof document !== 'undefined' ? document.fullscreenElement : null;
  return element?.classList?.contains('model-3d') ? element : null;
}
function rotateFullscreenModel(delta) {
  const model = fullscreenModelElement();
  if (!model) return;
  const angle = Number.parseFloat(model.style.getPropertyValue('--model-angle')) || 0;
  model.style.setProperty('--model-angle', `${angle + delta}deg`);
}
function pollFullscreenModelGamepad() {
  const model = fullscreenModelElement();
  const pad = model && navigator.getGamepads ? [...navigator.getGamepads()].find(Boolean) : null;
  if (!model || !pad) { fullscreenPadPrev = {}; return; }
  const left = Boolean(pad.buttons[14]?.pressed || pad.axes[0] < -0.6);
  const right = Boolean(pad.buttons[15]?.pressed || pad.axes[0] > 0.6);
  if (left && !fullscreenPadPrev.left) rotateFullscreenModel(-15);
  else if (right && !fullscreenPadPrev.right) rotateFullscreenModel(15);
  fullscreenPadPrev = { left, right };
}
registerGamepadSurface({
  priority: 5,
  isActive: () => Boolean(fullscreenModelElement()),
  tick: pollFullscreenModelGamepad,
  wantsLoop: true,
});
if (typeof document !== 'undefined') {
  document.addEventListener('fullscreenchange', () => {
    fullscreenPadPrev = {};
    syncGamepadLoop();
  });
}
