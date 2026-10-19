/* Pointer, wheel, trackball, and spinner navigation for Big Box.
   The surface owns no game state: steps and page turns are callbacks. */

function clampSensitivity(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(20, Math.max(1, number)) : 6;
}

function ignoredTarget(target) {
  return Boolean(target?.closest?.('input,select,textarea,button,a'));
}

export function createPointerNavigator({
  surface,
  sensitivity = 6,
  onStep = () => {},
  onPage = () => {},
  onActivate = () => {},
  onActivity = () => {},
  wheelEnabled = true,
  dragEnabled = true,
}) {
  if (!surface) return { destroy() {} };
  const scale = clampSensitivity(sensitivity);
  const wheelStepSize = 40 / scale;
  // Drag distance uses the same sensitivity scale as the wheel: higher
  // sensitivity means a shorter gesture per step.
  const dragStepSize = 12 / scale;
  let wheelAccumulator = 0;
  let wheelDirection = 0;
  let drag = null;
  let suppressClick = false;

  const observe = () => {
    try { onActivity(); } catch { /* activity is advisory */ }
  };

  const onWheel = event => {
    if (!wheelEnabled || ignoredTarget(event.target)) return;
    observe();
    const modeScale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
    const deltaX = Number(event.deltaX || 0) * modeScale;
    const deltaY = Number(event.deltaY || 0) * modeScale;
    const delta = Math.abs(deltaX) > Math.abs(deltaY) ? deltaX : deltaY;
    if (!delta) return;
    if (event.shiftKey) {
      event.preventDefault();
      wheelAccumulator = 0;
      wheelDirection = 0;
      onPage(Math.sign(delta));
      return;
    }
    const direction = Math.sign(delta);
    if (direction !== wheelDirection) {
      wheelAccumulator = 0;
      wheelDirection = direction;
    }
    wheelAccumulator += Math.abs(delta);
    if (wheelAccumulator < wheelStepSize) return;
    event.preventDefault();
    wheelAccumulator = 0;
    onStep(direction, 'wheel');
  };

  const onPointerDown = event => {
    if (!dragEnabled || event.pointerType !== 'mouse' || event.button !== 0 || ignoredTarget(event.target)) return;
    drag = { id: event.pointerId, startX: event.clientX, startY: event.clientY, moved: false };
    suppressClick = false;
    try { surface.setPointerCapture?.(event.pointerId); } catch { /* optional */ }
  };

  const onPointerMove = event => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.abs(dx) <= 10) return;
    if (!drag.moved && Math.abs(dy) > Math.abs(dx)) return;
    observe();
    drag.moved = true;
    suppressClick = true;
    const steps = Math.trunc(Math.abs(dx) / dragStepSize);
    if (!steps) return;
    const direction = Math.sign(dx);
    for (let index = 0; index < steps; index++) onStep(direction, 'drag');
    drag.startX += direction * steps * dragStepSize;
  };

  const finishDrag = event => {
    if (!drag || event.pointerId !== drag.id) return;
    const moved = drag.moved;
    drag = null;
    try { surface.releasePointerCapture?.(event.pointerId); } catch { /* optional */ }
    if (!moved && !ignoredTarget(event.target)) onActivate();
  };

  const onClick = event => {
    if (!suppressClick) return;
    suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  };

  surface.addEventListener('wheel', onWheel, { passive: false });
  surface.addEventListener('pointerdown', onPointerDown);
  surface.addEventListener('pointermove', onPointerMove);
  surface.addEventListener('pointerup', finishDrag);
  surface.addEventListener('pointercancel', finishDrag);
  surface.addEventListener('click', onClick, true);

  return {
    destroy() {
      surface.removeEventListener('wheel', onWheel);
      surface.removeEventListener('pointerdown', onPointerDown);
      surface.removeEventListener('pointermove', onPointerMove);
      surface.removeEventListener('pointerup', finishDrag);
      surface.removeEventListener('pointercancel', finishDrag);
      surface.removeEventListener('click', onClick, true);
    },
  };
}
