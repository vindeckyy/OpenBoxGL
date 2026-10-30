// Facet counts must be identical wherever they are computed: the search
// worker (static/worker.search.js) and the UI thread (static/pure.js). The
// filter chips show whichever arrived last, so a divergence would put two
// different numbers on screen for the same library.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { facetCounts, facetCountsFromPlatforms } from '../../static/pure.js';

const require = createRequire(import.meta.url);
const worker = require('../../static/worker.search.js');

const corpus = [
  { name: 'Alpha', platform: 'NES' },
  { name: 'Beta', platform: 'NES' },
  { name: 'Gamma', platform: 'SNES' },
  { name: 'Delta', platform: '' },
  { name: 'Epsilon' },
];

const categories = { NES: 'Console', SNES: 'Console' };

test('the worker and the UI thread count facets identically', () => {
  assert.deepEqual(worker.facetCounts(corpus, categories), facetCounts(corpus, categories));
});

test('the worker counts the platform values it is sent', () => {
  const platforms = corpus.map(game => game.platform);
  assert.deepEqual(
    worker.facetCountsFromPlatforms(platforms, categories),
    facetCountsFromPlatforms(platforms, categories),
  );
  assert.deepEqual(
    worker.facetCountsFromPlatforms(platforms, categories),
    worker.facetCounts(corpus, categories),
  );
});

test('the worker counts a missing platform as Unspecified and an unknown category as Other', () => {
  const counts = worker.facetCounts(corpus, categories);
  assert.equal(counts.platform['Unspecified'], 2);
  assert.equal(counts.platform.NES, 2);
  assert.equal(counts.platform.SNES, 1);
  assert.equal(counts.platform_category.Other, 2);
  assert.equal(counts.platform_category.Console, 3);
});

test('the worker counts an empty library as nothing', () => {
  const counts = worker.facetCounts([], categories);
  assert.deepEqual(counts.platform, {});
  assert.deepEqual(counts.platform_category, {});
});
