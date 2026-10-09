// Behavioural tests for static/pure.js, the DOM-free helpers the 1.16.1 sweep
// fixed. Run with: node --test tests/js/   (CI runs this in the gate job).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { progressScore, importedCount, formatHours, nextRetryDelay, stableIdFor } from '../../static/pure.js';

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
