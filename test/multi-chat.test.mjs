import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyPreset, closePane, serialize, deserialize, firstOpenPane,
} from '../static/multi-chat.js';

test('save (serialize): two open panes round-trip to JSON with preset and both session ids', () => {
  const state = { preset: '2col', panes: ['sess-a', 'sess-b'] };
  const json = serialize(state);
  assert.ok(typeof json === 'string');
  assert.ok(json.includes('preset'), 'serialized JSON must carry the preset');
  assert.ok(json.includes('2col'));
  assert.ok(json.includes('sess-a'), 'serialized JSON must carry the first session id');
  assert.ok(json.includes('sess-b'), 'serialized JSON must carry the second session id');
  // and it must be exactly the state, so a round-trip is lossless
  assert.deepEqual(JSON.parse(json), { preset: '2col', panes: ['sess-a', 'sess-b'] });
});

test('restore (deserialize): stored 2col layout comes back as two panes; malformed JSON falls back to default', () => {
  const stored = serialize({ preset: '2col', panes: ['sess-a', 'sess-b'] });
  const restored = deserialize(stored);
  assert.equal(restored.preset, '2col');
  assert.deepEqual(restored.panes, ['sess-a', 'sess-b']);
  assert.equal(restored.panes.length, 2);

  // malformed / invalid shapes never throw and fall back to the single-pane default
  assert.deepEqual(deserialize('not json at all {'), { preset: '1', panes: [null] });
  assert.deepEqual(deserialize(JSON.stringify({ preset: 'nonsense', panes: ['x'] })), { preset: '1', panes: [null] });
  assert.deepEqual(deserialize('["sess-a"]'), { preset: '1', panes: [null] });
});

test('applyPreset(1): collapses to exactly one pane, the first open session', () => {
  const state = { preset: '2x2', panes: ['a', 'b', 'c', 'd'] };
  const out = applyPreset(state, '1');
  assert.equal(out.panes.length, 1);
  assert.equal(out.panes[0], 'a');
  assert.equal(out.preset, '1');
  assert.equal(firstOpenPane(out), 'a');
  // with no open sessions at all, the single slot is an empty placeholder
  assert.deepEqual(applyPreset({ preset: '2x2', panes: [null, null] }, '1'), { preset: '1', panes: [null] });
});

test('applyPreset(2x2): four open sessions yield four panes', () => {
  const state = { preset: '1', panes: ['a', 'b', 'c', 'd'] };
  const out = applyPreset(state, '2x2');
  assert.equal(out.panes.length, 4);
  assert.deepEqual(out.panes, ['a', 'b', 'c', 'd']);
  assert.equal(out.preset, '2x2');
});

test('closePane: removes the pane and returns a next id without ending the session', async (t) => {
  // Stub the only session-ending path (POST .../close) with a spy on globalThis.
  // If anything in the pure path tried to end a session it would have to go
  // through fetch; assert fetch is never called for a close.
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (url, opts) => {
    calls.push(String(url));
    return realFetch ? realFetch(url, opts) : Promise.reject(new Error('no network in tests'));
  };
  t.after(() => { globalThis.fetch = realFetch; });

  const state = { preset: '2col', panes: ['sess-a', 'sess-b'] };
  const result = closePane(state, 'sess-a');
  assert.ok(!result.state.panes.includes('sess-a'), 'closed pane must be gone');
  assert.deepEqual(result.state.panes, ['sess-b']);
  assert.equal(result.state.preset, '2col');
  assert.equal(result.nextFocus, 'sess-b', 'next id is the most recent remaining open pane');

  const last = closePane(result.state, 'sess-b');
  assert.equal(last.nextFocus, null, 'no remaining open pane -> no next id');
  assert.deepEqual(last.state.panes, [null]);

  await new Promise((r) => setImmediate(r)); // let any (forbidden) fetch queue drain
  assert.deepEqual(calls, [], 'closing a pane must never fetch / end the session');
  globalThis.fetch = realFetch;
});

test('no shared state: two independently created layout states do not affect each other', () => {
  const a = applyPreset({ preset: '1', panes: ['x', 'y', 'z'] }, '2col');
  const b = applyPreset({ preset: '1', panes: ['x', 'y', 'z'] }, '2x2');
  assert.deepEqual(a, { preset: '2col', panes: ['x', 'y'] });
  assert.deepEqual(b, { preset: '2x2', panes: ['x', 'y', 'z', null] });

  const ca = closePane(a, 'x').state;
  const cb = closePane(b, 'z').state;
  assert.deepEqual(ca.panes, ['y']);
  assert.deepEqual(cb.panes, ['x', 'y', null]);
  assert.notEqual(a, ca);

  // deserialized copies are independent of the stored input too
  const stored = serialize({ preset: '2col', panes: ['p1', 'p2'] });
  const s1 = deserialize(stored);
  const s2 = deserialize(stored);
  s1.panes.push('p3');
  assert.deepEqual(s2.panes, ['p1', 'p2']);
});
