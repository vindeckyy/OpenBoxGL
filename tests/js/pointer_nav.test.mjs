// Behavioural tests for static/pointer_nav.js. The navigator is DOM-free
// apart from addEventListener/removeEventListener, so a small fake surface
// exercises the wheel and drag accumulators without a browser.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createPointerNavigator } from '../../static/pointer_nav.js';

function fakeSurface() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, handler) {
      const list = listeners.get(type) || [];
      list.push(handler);
      listeners.set(type, list);
    },
    removeEventListener(type, handler) {
      listeners.set(type, (listeners.get(type) || []).filter(item => item !== handler));
    },
    setPointerCapture() {},
    releasePointerCapture() {},
    emit(type, event) {
      for (const handler of listeners.get(type) || []) handler(event);
    },
  };
}

const target = { closest: () => null };

test('drag uses 12 / sensitivity pixels per step', () => {
  const surface = fakeSurface();
  const steps = [];
  createPointerNavigator({ surface, sensitivity: 6, onStep: (direction, source) => steps.push([direction, source]) });
  surface.emit('pointerdown', { pointerType: 'mouse', button: 0, pointerId: 1, clientX: 0, clientY: 0, target });
  surface.emit('pointermove', { pointerId: 1, clientX: 24, clientY: 0, target });
  assert.equal(steps.length, 12);
  assert.ok(steps.every(([direction, source]) => direction === 1 && source === 'drag'));
});

test('wheel accumulates to 40 / sensitivity and resets on direction change', () => {
  const surface = fakeSurface();
  const steps = [];
  createPointerNavigator({ surface, sensitivity: 10, onStep: (direction, source) => steps.push([direction, source]) });
  const wheel = deltaY => ({ deltaY, deltaX: 0, deltaMode: 0, shiftKey: false, target, preventDefault() {} });
  surface.emit('wheel', wheel(2));
  surface.emit('wheel', wheel(1));
  assert.equal(steps.length, 0);
  surface.emit('wheel', wheel(1));
  assert.deepEqual(steps, [[1, 'wheel']]);
  surface.emit('wheel', wheel(-2));
  assert.equal(steps.length, 1);
  surface.emit('wheel', wheel(-2));
  assert.deepEqual(steps[1], [-1, 'wheel']);
});

test('shift+wheel pages and suppress happens only after a drag', () => {
  const surface = fakeSurface();
  const pages = [];
  const steps = [];
  createPointerNavigator({ surface, sensitivity: 6, onStep: (direction, source) => steps.push([direction, source]), onPage: direction => pages.push(direction) });
  surface.emit('wheel', { deltaY: 120, deltaX: 0, deltaMode: 0, shiftKey: true, target, preventDefault() {} });
  assert.deepEqual(pages, [1]);
  assert.equal(steps.length, 0);
  let prevented = false;
  surface.emit('pointerdown', { pointerType: 'mouse', button: 0, pointerId: 2, clientX: 0, clientY: 0, target });
  surface.emit('pointermove', { pointerId: 2, clientX: 30, clientY: 0, target });
  surface.emit('click', { preventDefault() { prevented = true; }, stopPropagation() {} });
  assert.equal(prevented, true);
});
