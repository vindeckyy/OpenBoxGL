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

// Exponential backoff for a dropped event stream, capped at max.
export function nextRetryDelay(current, max) {
  return Math.min(current * 2, max);
}

// The stable id for a row's array index, or undefined when the row is gone.
export function stableIdFor(games, index) {
  return (Array.isArray(games) ? games : []).find(game => game?.id === index)?.game_id ?? undefined;
}
