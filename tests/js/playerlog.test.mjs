// Unit tests for the factored 1200x630 share card layout helpers in static/pure.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { shareCardLayout, wrapWordsToWidth } from '../../static/pure.js';

test('shareCardLayout computes margins with and without artwork', () => {
  const withArt = shareCardLayout({ hasImage: true, titleLineCount: 2 });
  assert.equal(withArt.width, 1200);
  assert.equal(withArt.height, 630);
  assert.equal(withArt.imageWidth, 420);
  assert.equal(withArt.left, 484);
  assert.equal(withArt.maxWidth, 652);
  assert.equal(withArt.subtitleY, 148 + 2 * 56 + 18);

  const noArt = shareCardLayout({ hasImage: false, titleLineCount: 1 });
  assert.equal(noArt.imageWidth, 0);
  assert.equal(noArt.left, 64);
  assert.equal(noArt.maxWidth, 1072);
});

test('wrapWordsToWidth breaks lines strictly at maxWidth boundaries', () => {
  const lines = wrapWordsToWidth(
    'One hundred hours in Chrono Trigger',
    candidate => candidate.length * 10,
    140,
  );
  assert.deepEqual(lines, ['One hundred', 'hours in', 'Chrono Trigger']);
  assert.deepEqual(wrapWordsToWidth('   ', () => 10, 100), []);
});
