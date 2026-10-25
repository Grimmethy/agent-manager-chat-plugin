import test from 'node:test';
import assert from 'node:assert/strict';
import { attentionStep, shouldChime } from '../static/attention-view.js';

test('first call (prev null): no newlyWaiting, count and map are right', () => {
  const { map, newlyWaiting, count } = attentionStep(null, [{ id: 'a', status: 'waiting' }]);
  assert.equal(newlyWaiting.length, 0);
  assert.equal(count, 1);
  assert.equal(map.a, 'waiting');
});

test('idle to waiting transition is newly waiting', () => {
  const { map, newlyWaiting, count } = attentionStep({ a: 'idle' }, [{ id: 'a', status: 'waiting' }]);
  assert.deepEqual(newlyWaiting, ['a']);
  assert.equal(count, 1);
  assert.equal(map.a, 'waiting');
});

test('already waiting stays quiet', () => {
  const { newlyWaiting, count } = attentionStep({ a: 'waiting' }, [{ id: 'a', status: 'waiting' }]);
  assert.deepEqual(newlyWaiting, []);
  assert.equal(count, 1);
});

test('a row disappearing: its key leaves the new map, no false newlyWaiting', () => {
  const prev = { a: 'waiting', b: 'idle' };
  const { map, newlyWaiting, count } = attentionStep(prev, [{ id: 'a', status: 'waiting' }]);
  assert.deepEqual(Object.keys(map), ['a']);
  assert.deepEqual(newlyWaiting, []); // a was already waiting
  assert.equal(count, 1);
  // And a row that was waiting before, now idle: not newly waiting.
  const again = attentionStep(prev, [{ id: 'b', status: 'idle' }]);
  assert.deepEqual(again.newlyWaiting, []);
  assert.equal(again.count, 0);
});

test('a key absent from prev is newly waiting (but not on first call)', () => {
  const { newlyWaiting } = attentionStep({ a: 'idle' }, [{ id: 'b', status: 'waiting' }]);
  assert.deepEqual(newlyWaiting, ['b']);
});

test('count reflects all currently waiting rows', () => {
  const rows = [
    { id: 'a', status: 'waiting' },
    { id: 'b', status: 'idle' },
    { id: 'c', status: 'waiting' },
  ];
  const { count } = attentionStep(null, rows);
  assert.equal(count, 2);
});

test('host-keyed rows use host/id as the key', () => {
  const { map, newlyWaiting } = attentionStep(null, [{ host: 'h1', id: 'a', status: 'waiting' }]);
  assert.deepEqual(Object.keys(map), ['h1/a']);
  assert.equal(map['h1/a'], 'waiting');
  const again = attentionStep({ 'h1/a': 'idle' }, [{ host: 'h1', id: 'a', status: 'waiting' }]);
  assert.deepEqual(again.newlyWaiting, ['h1/a']);
});

test('shouldChime is false when disabled, even with an off-screen newly waiting key', () => {
  assert.equal(shouldChime(['x'], [], false), false);
  assert.equal(shouldChime(['x'], ['x'], false), false);
});

test('shouldChime is false when enabled and the newly waiting pane is on screen', () => {
  assert.equal(shouldChime(['x'], ['x'], true), false);
  assert.equal(shouldChime(['x', 'y'], ['x', 'y'], true), false);
  assert.equal(shouldChime([], ['x'], true), false);
});

test('shouldChime is true when enabled and some newly waiting pane is off screen', () => {
  assert.equal(shouldChime(['x'], [], true), true);
  assert.equal(shouldChime(['x', 'y'], ['x'], true), true);
});
