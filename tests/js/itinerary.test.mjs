// Unit tests for Backlog Itinerary capacity & week bucket helpers in static/pure.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { itineraryCapacityHours, itineraryWeekFor } from '../../static/pure.js';

test('itineraryCapacityHours clamps horizon and daily pace correctly', () => {
  assert.equal(itineraryCapacityHours(30, 60), 30);
  assert.equal(itineraryCapacityHours(14, 90), 21);
  assert.equal(itineraryCapacityHours(1, 1), 0.58); // clamped to 7d * 5m = 35m = 0.58h
  assert.equal(itineraryCapacityHours(999, 2000), 5840); // clamped to 365d * 960m = 5840h
});

test('itineraryWeekFor assigns 1-based weeks capped at maxWeeks', () => {
  assert.equal(itineraryWeekFor(0, 7, 4), 1);
  assert.equal(itineraryWeekFor(6.9, 7, 4), 1);
  assert.equal(itineraryWeekFor(7.0, 7, 4), 2);
  assert.equal(itineraryWeekFor(40, 7, 4), 4);
});
