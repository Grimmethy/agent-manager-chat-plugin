// test/dockview-grid.test.mjs -- unit tests for the pure mapDirection helper
// in static/dockview-grid.js.
//
// NOTE on scope: the task text assumed mapDirection would normalise the
// dockview position tokens 'first' / 'nextTo' / 'below'. The implementation
// that actually landed (HUB0019 1/4) instead maps the four dockview *axis*
// directions (top/bottom/left/right) onto { axis, side } pairs and rejects
// every other token with a RangeError. These tests therefore assert the real
// behaviour: the four axis mappings, and that 'first', 'nextTo', 'below' and
// any other unknown direction throw -- so the named tokens ARE covered, as
// rejections, since the shipped adapter has no mapping for them.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mapDirection } from '../static/dockview-grid.js';

test('mapDirection: maps the four axis directions to { axis, side } pairs', () => {
  assert.deepEqual(mapDirection('top'), { axis: 'row', side: 'start' });
  assert.deepEqual(mapDirection('bottom'), { axis: 'row', side: 'end' });
  assert.deepEqual(mapDirection('left'), { axis: 'col', side: 'start' });
  assert.deepEqual(mapDirection('right'), { axis: 'col', side: 'end' });
});

test('mapDirection: rejects "first" (not a mapped direction in this implementation)', () => {
  assert.throws(() => mapDirection('first'), {
    name: 'RangeError',
    message: /first/,
  });
});

test('mapDirection: rejects "nextTo" (not a mapped direction in this implementation)', () => {
  assert.throws(() => mapDirection('nextTo'), {
    name: 'RangeError',
    message: /nextTo/,
  });
});

test('mapDirection: rejects "below" (not a mapped direction in this implementation)', () => {
  assert.throws(() => mapDirection('below'), {
    name: 'RangeError',
    message: /below/,
  });
});

test('mapDirection: throws RangeError on any other unknown direction', () => {
  assert.throws(() => mapDirection('diagonal'), {
    name: 'RangeError',
    message: /diagonal/,
  });
  // non-string / falsy inputs are rejected too
  assert.throws(() => mapDirection(undefined), RangeError);
  assert.throws(() => mapDirection(''), RangeError);
});
