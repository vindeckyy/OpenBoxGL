/* Unified gamepad loop — exactly one rAF poll loop for the whole app.
 *
 * Surfaces register a {priority, isActive, tick, wantsLoop} handler; each
 * frame the loop dispatches to the highest-priority active surface:
 *   Big Box pause overlay (10) → arcade room (20) → Big Box (30) → library (40).
 * wantsLoop keeps the loop alive for surfaces that need frames with no pad
 * connected (Big Box attract/screensaver timer); otherwise pad presence
 * drives it. Surfaces own their input handling — this module only owns the
 * frame.
 */

const surfaces = [];
let gamepadFrame = 0;

/** Register a surface's gamepad handling. Lower priority number wins. */
export function registerGamepadSurface({ priority, isActive, tick, wantsLoop = false }) {
  surfaces.push({ priority, isActive, tick, wantsLoop });
  surfaces.sort((a, b) => a.priority - b.priority);
}

function anyGamepadConnected() {
  try {
    return Boolean(navigator.getGamepads && [...navigator.getGamepads()].filter(Boolean).length);
  } catch {
    return false;
  }
}

function safeActive(surface) {
  try {
    return Boolean(surface.isActive());
  } catch {
    return false;
  }
}

function loopWanted() {
  if (typeof document !== 'undefined' && document.hidden) return false;
  if (anyGamepadConnected()) return true;
  return surfaces.some(surface => surface.wantsLoop && safeActive(surface));
}

function tick() {
  gamepadFrame = 0;
  if (!loopWanted()) return;
  const surface = surfaces.find(safeActive);
  if (surface) {
    try {
      surface.tick();
    } catch {
      // One surface must not kill the loop for the others.
    }
  }
  gamepadFrame = requestAnimationFrame(tick);
}

/** Start the loop if something needs it; safe to call from any lifecycle event. */
export function ensureGamepadLoop() {
  if (!gamepadFrame && loopWanted() && typeof requestAnimationFrame === 'function') {
    gamepadFrame = requestAnimationFrame(tick);
  }
}

/** Stop the loop unconditionally. */
export function stopGamepadLoop() {
  if (gamepadFrame) cancelAnimationFrame(gamepadFrame);
  gamepadFrame = 0;
}

/** Re-evaluate: run while a surface needs the loop, stop otherwise. */
export function syncGamepadLoop() {
  if (loopWanted()) ensureGamepadLoop();
  else stopGamepadLoop();
}
