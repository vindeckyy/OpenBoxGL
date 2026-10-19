// Behavioural tests for static/pure.js, the DOM-free helpers the 1.16.1 sweep
// fixed. Run with: node --test tests/js/   (CI runs this in the gate job).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { progressScore, importedCount, formatHours, formatMoney, nextRetryDelay, stableIdFor, cycleFilterState, sourceFilterMatches, firstEdge, wheelStepAllowed, matchesKey, integerScale, screenSaverStats, clampAngle } from '../../static/pure.js';

test('formatMoney formats cents and falls back for an invalid currency', () => {
  assert.equal(formatMoney(1999, 'USD', 'en-US'), '$19.99');
  assert.equal(formatMoney(0, 'USD', 'en-US'), '$0.00');
  assert.equal(formatMoney(100, 'ZZZZ', 'en-US'), 'ZZZZ 1.00');
  assert.equal(formatMoney(Number.NaN, 'USD', 'en-US'), '');
});

test('progressScore reads labels and numbers, never NaN', () => {
  assert.equal(progressScore('Beaten'), 1);
  assert.equal(progressScore('mastered'), 3);
  assert.equal(progressScore('Playing'), 0.1);
  assert.equal(progressScore('2.5'), 2.5);
  assert.equal(progressScore(4), 4);
  assert.equal(progressScore(Number.NaN), 0);
  assert.equal(progressScore(Infinity), 0);
  assert.equal(progressScore(undefined), 0);
  assert.equal(progressScore('Unplayed'), 0);
});

test('importedCount counts the preview when the result is the same preview', () => {
  const preview = { added: 7 };
  assert.equal(importedCount(preview, preview), 7);
});

test('importedCount adds the wizard result on top of the preview', () => {
  // The wizard's own call reports added: 0 once the preview committed the games.
  assert.equal(importedCount({ added: 7 }, { added: 2 }), 9);
  assert.equal(importedCount({ added: 7 }, { added: 0 }), 7);
  assert.equal(importedCount({}, { added: 'x' }), 0);
});

test('formatHours switches units at the boundaries', () => {
  assert.equal(formatHours(0), '0h');
  assert.equal(formatHours(1800), '30m');
  assert.equal(formatHours(3600), '1.0h');
  assert.equal(formatHours(5 * 3600), '5.0h');
  assert.equal(formatHours(12 * 3600), '12h');
});

test('a momentum drop is formatted by its magnitude, as insights.js does', () => {
  // A raw negative value falls into the "under an hour" branch and reads as minutes.
  const dropSeconds = -5 * 3600;
  assert.equal(formatHours(-dropSeconds), '5.0h');
});

test('nextRetryDelay doubles and stops at the cap', () => {
  assert.equal(nextRetryDelay(3000, 60000), 6000);
  assert.equal(nextRetryDelay(6000, 60000), 12000);
  assert.equal(nextRetryDelay(48000, 60000), 60000);
  assert.equal(nextRetryDelay(60000, 60000), 60000);
});

test('stableIdFor returns the stable id of the row at an index', () => {
  const games = [{ id: 0, game_id: 'g-a' }, { id: 1, game_id: 'g-b' }];
  assert.equal(stableIdFor(games, 1), 'g-b');
  assert.equal(stableIdFor(games, 9), undefined);
  assert.equal(stableIdFor(undefined, 0), undefined);
  assert.equal(stableIdFor([null, { id: 2 }], 2), undefined);
});

test('cycleFilterState goes none -> include -> exclude -> none', () => {
  let state = cycleFilterState('platform', 'SNES', {});
  assert.deepEqual(state, { include: ['SNES'], exclude: [] });
  state = cycleFilterState('platform', 'SNES', state);
  assert.deepEqual(state, { include: [], exclude: ['SNES'] });
  state = cycleFilterState('platform', 'SNES', state);
  assert.deepEqual(state, { include: [], exclude: [] });
});

test('cycleFilterState reads a field map when passed one', () => {
  const state = cycleFilterState('platform', 'SNES', { platform: { include: [], exclude: ['SNES'] }, category: {} });
  assert.deepEqual(state, { include: [], exclude: [] });
});

test('sourceFilterMatches treats an excluded source as a hide rule, not an include', () => {
  assert.equal(sourceFilterMatches('Steam', '', []), true);
  assert.equal(sourceFilterMatches('Steam', 'Steam', []), true);
  assert.equal(sourceFilterMatches('GOG', 'Steam', []), false);
  // The exclusion pass removes Steam, but every other source must still show.
  assert.equal(sourceFilterMatches('Steam', 'Steam', ['Steam']), true);
  assert.equal(sourceFilterMatches('GOG', 'Steam', ['Steam']), true);
  assert.equal(sourceFilterMatches('Steam', 'Steam', ['GOG']), true);
  assert.equal(sourceFilterMatches('GOG', 'Steam', ['GOG']), false);
});

test('firstEdge returns exactly the first assigned action', () => {
  const edges = { favorite: true, play: true, back: true };
  assert.equal(firstEdge(edges, ['pause', 'menu', 'back', 'play', 'favorite']), 'back');
  assert.equal(firstEdge({}, ['play']), null);
});

test('wheelStepAllowed honors speed with a floor of 120ms at 1x', () => {
  assert.equal(wheelStepAllowed(100, 0, 1), false);
  assert.equal(wheelStepAllowed(121, 0, 1), true);
  assert.equal(wheelStepAllowed(59, 0, 2), false);
  assert.equal(wheelStepAllowed(61, 0, 2), true);
  assert.equal(wheelStepAllowed(1000, 0, 0), true);
});

test('matchesKey compares modifiers and named/single-character keys', () => {
  assert.equal(matchesKey({ key: 'Enter', ctrlKey: false, altKey: false, shiftKey: false }, 'Enter'), true);
  assert.equal(matchesKey({ key: ',', ctrlKey: true, altKey: false, shiftKey: false }, 'Ctrl+,'), true);
  assert.equal(matchesKey({ key: ',', ctrlKey: false, altKey: false, shiftKey: false }, 'Ctrl+,'), false);
  assert.equal(matchesKey({ key: 'f', ctrlKey: true, altKey: false, shiftKey: false }, 'f'), false);
  assert.equal(matchesKey({ key: ' ', ctrlKey: false, altKey: false, shiftKey: false }, 'Space'), true);
  assert.equal(matchesKey({ key: 'ArrowLeft', ctrlKey: false, altKey: false, shiftKey: false }, 'ArrowLeft'), true);
  // Physical names accepted by the settings API still match event.key.
  assert.equal(matchesKey({ key: 'a', ctrlKey: true, altKey: false, shiftKey: false }, 'Ctrl+KeyA'), true);
  assert.equal(matchesKey({ key: '0', ctrlKey: false, altKey: false, shiftKey: false }, 'Digit0'), true);
  assert.equal(matchesKey({ key: '1', ctrlKey: false, altKey: false, shiftKey: false }, 'Digit0'), false);
});

test('integerScale never upsamples and clamps to eight', () => {
  assert.equal(integerScale(100, 100, 50, 50), 1);
  assert.equal(integerScale(100, 100, 300, 300), 3);
  assert.equal(integerScale(10, 10, 1000, 1000), 8);
  assert.equal(integerScale(0, 100, 500, 500), 1);
});

test('screenSaverStats joins achievements and time to beat', () => {
  assert.equal(screenSaverStats(null), '');
  assert.equal(screenSaverStats({ra_achievements_total: 10, ra_achievements_earned: 3, ra_progress_pct: 30, time_to_beat_hours: 20}), '3/10 achievements · 30% · ~20h to beat');
  assert.equal(screenSaverStats({ra_achievements_total: 10, ra_achievements_earned: 10, ra_mastered: true}), '10/10 achievements · 100% · Mastered');
  assert.equal(screenSaverStats({time_to_beat_hours: 2.5}), '~2.5h to beat');
});

test('clampAngle locks axes and bounds vertical rotation', () => {
  assert.equal(clampAngle(120, 'free', 'x'), 90);
  assert.equal(clampAngle(-120, 'free', 'x'), -90);
  assert.equal(clampAngle(120, 'free', 'y'), 120);
  assert.equal(clampAngle(45, 'horizontal', 'x'), 0);
  assert.equal(clampAngle(45, 'horizontal', 'y'), 45);
  assert.equal(clampAngle(45, 'vertical', 'y'), 0);
  assert.equal(clampAngle(45, 'vertical', 'x'), 45);
});
