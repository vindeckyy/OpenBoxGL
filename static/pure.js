/* DOM-free helpers. Nothing here may import app state, touch the DOM or call
   the network, so node --test can exercise the logic directly
   (tests/js/pure.test.mjs). Modules import from here; do not add side effects. */

// Mirrors handlers/moments.py::auto_moment_trigger.progress_score. Progress is
// usually a label ("Beaten"); Number("Beaten") is NaN, so the progress trigger
// could never fire from the client.
const PROGRESS_LABEL_SCORES = { playing: 0.1, started: 0.1, beaten: 1, completed: 2, mastered: 3 };

export function progressScore(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
  const text = String(value ?? '').trim();
  if (text && Number.isFinite(Number(text))) return Number(text);
  return PROGRESS_LABEL_SCORES[text.toLowerCase()] || 0;
}

// POST /api/import commits the scan (there is no dry-run mode), so by the
// time the wizard call re-scans with the chosen emulators every game is
// already in the library and the wizard reports added: 0. The real count is
// the preview's, plus anything the wizard found on top of it.
export function importedCount(preview, result) {
  const first = Number(preview?.added) || 0;
  return result === preview ? first : first + (Number(result?.added) || 0);
}

// formatHours() expects a magnitude; callers pass |delta| and add the sign.
export function formatHours(seconds) {
  if (!seconds) return '0h';
  const hours = seconds / 3600;
  if (hours < 1) return `${Math.round(seconds / 60)}m`;
  if (hours < 10) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours)}h`;
}

// Currency formatting for price-paid surfaces. Invalid codes fall back to the
// code and a fixed two-decimal amount instead of throwing in render.
export function formatMoney(cents, currency, locale) {
  const amount = Number(cents);
  if (!Number.isFinite(amount)) return '';
  const code = String(currency || '').toUpperCase();
  try {
    return new Intl.NumberFormat(locale || undefined, { style: 'currency', currency: code }).format(amount / 100);
  } catch {
    return `${code} ${(amount / 100).toFixed(2)}`.trim();
  }
}

// Exponential backoff for a dropped event stream, capped at max.
export function nextRetryDelay(current, max) {
  return Math.min(current * 2, max);
}

// The stable id for a row's array index, or undefined when the row is gone.
export function stableIdFor(games, index) {
  return (Array.isArray(games) ? games : []).find(game => game?.id === index)?.game_id ?? undefined;
}

// Cycle one filter value none -> include -> exclude -> none. `current` may be
// either that field's {include, exclude} state or a map keyed by field.
export function cycleFilterState(field, value, current = {}) {
  const scoped = current && (Array.isArray(current.include) || Array.isArray(current.exclude))
    ? current
    : current?.[field] || {};
  const include = new Set(scoped.include || []);
  const exclude = new Set(scoped.exclude || []);
  if (include.has(value)) {
    include.delete(value);
    exclude.add(value);
  } else if (exclude.has(value)) {
    exclude.delete(value);
  } else {
    include.add(value);
  }
  return { include: [...include], exclude: [...exclude] };
}

/**
 * Source include/exclude predicate.
 *
 * An excluded selected source must not also act as an include filter, or the
 * view becomes empty: the exclusion matcher removes the only games that
 * matched the include. When the selected source is excluded, skip the include
 * test and let `gameExcluded` remove that source.
 */
export function sourceFilterMatches(source, selected, excluded = []) {
  if (!selected) return true;
  const selectedValue = String(selected).toLowerCase();
  const isExcluded = (Array.isArray(excluded) ? excluded : [excluded])
    .some(value => String(value).toLowerCase() === selectedValue);
  if (isExcluded) return true;
  return String(source || '').toLowerCase() === selectedValue;
}

// One gamepad button must fire one action. First listed wins.
export function firstEdge(edgeMap, order) {
  return (order || []).find(action => Boolean(edgeMap?.[action])) || null;
}

// Big Box wheel throttle. Page moves bypass this.
export function wheelStepAllowed(now, last, speed) {
  const factor = Number(speed);
  const clamped = Number.isFinite(factor) ? Math.min(3, Math.max(0.25, factor)) : 1;
  return Number(now) - Number(last || 0) >= 120 / clamped;
}

// Integer nearest-neighbour scale for a viewed image, clamped to 1–8.
export function integerScale(naturalWidth, naturalHeight, viewWidth, viewHeight) {
  const nw = Number(naturalWidth), nh = Number(naturalHeight);
  const vw = Number(viewWidth), vh = Number(viewHeight);
  if (!(nw > 0 && nh > 0 && vw > 0 && vh > 0)) return 1;
  return Math.min(8, Math.max(1, Math.floor(Math.min(vw / nw, vh / nh))));
}

export function screenSaverStats(game) {
  if (!game) return '';
  const parts = [];
  const total = Number(game.ra_achievements_total || 0);
  if (total > 0) {
    const earned = Number(game.ra_achievements_earned || 0);
    const pct = Math.round(Number(game.ra_progress_pct || (earned / total) * 100));
    parts.push(`${earned}/${total} achievements · ${pct}%${game.ra_mastered ? ' · Mastered' : ''}`);
  }
  const hours = Number(game.time_to_beat_hours);
  if (Number.isFinite(hours) && hours > 0) {
    parts.push(game.ra_mastered ? 'Mastered' : `~${formatHours(hours * 3600)} to beat`);
  }
  return parts.join(' · ');
}

// Clamp a model rotation. Horizontal locks the X axis, vertical the Y axis.
export function clampAngle(angle, lock = 'free', axis = 'x') {
  const value = Number(angle) || 0;
  if (lock === 'horizontal' && axis === 'x') return 0;
  if (lock === 'vertical' && axis === 'y') return 0;
  return axis === 'x' ? Math.max(-90, Math.min(90, value)) : value;
}

// Match a KeyboardEvent (or a plain {key,ctrlKey,...}) against a stored spec.
export const KEYBOARD_DEFAULTS = {
  play: 'Enter', back: 'Escape', favorite: 'f', random: 'r',
  page_left: 'PageUp', page_right: 'PageDown', pause: 'p', menu: 'm',
  search: '/', settings: 'Ctrl+,', reader_prev: 'ArrowLeft', reader_next: 'ArrowRight',
};

export function keyAction(settings, action) {
  return { ...KEYBOARD_DEFAULTS, ...(settings?.keyboard_map || {}) }[action];
}

export function matchesKey(event, spec) {
  if (!event || !spec) return false;
  const parts = String(spec).split('+');
  const target = parts.pop();
  const modifiers = new Set(parts.map(part => part.toLowerCase()));
  if (modifiers.has('ctrl') !== Boolean(event.ctrlKey)) return false;
  if (modifiers.has('alt') !== Boolean(event.altKey)) return false;
  if (modifiers.has('shift') !== Boolean(event.shiftKey)) return false;
  const actual = String(event.key ?? '');
  // The settings API accepts physical names (KeyA, Digit0) for callers that
  // use KeyboardEvent.code. Resolve them to the event.key spelling so an
  // accepted spec is always matchable.
  const physical = /^Key([A-Z])$/.exec(target);
  if (physical) return actual.toLowerCase() === physical[1].toLowerCase();
  const digit = /^Digit([0-9])$/.exec(target);
  if (digit) return actual === digit[1];
  if (target === 'Space') return actual === ' ';
  return target.length === 1
    ? actual.toLowerCase() === target.toLowerCase()
    : actual === target;
}
