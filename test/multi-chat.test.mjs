import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyPreset, closePane, serialize, deserialize, firstOpenPane, layoutForWidth,
  stripVisibleCount, clampStripIndex, stepStrip, stripLabel, stripButtons,
  DEFAULT_TEMPLATES, saveTemplate, applyTemplate, serializeTemplates, deserializeTemplates,
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

test('layoutForWidth: below the breakpoint collapses to the active pane in a 1-pane preset', () => {
  const state = { preset: '2x2', panes: ['a', 'b', 'c', 'd'] };
  assert.deepEqual(layoutForWidth(state, 'c', 400), { preset: '1', panes: ['c'] });
  assert.deepEqual(layoutForWidth(state, 'c', 759), { preset: '1', panes: ['c'] });
});

test('layoutForWidth: at or above the breakpoint returns the very same state object', () => {
  const state = { preset: '2col', panes: ['a', 'b'] };
  assert.equal(layoutForWidth(state, 'a', 760), state);
  assert.equal(layoutForWidth(state, 'a', 1200), state);
  assert.equal(layoutForWidth(state, 'a', 500, 400), state, 'a custom breakpoint is honoured');
  assert.deepEqual(layoutForWidth(state, 'b', 500, 600), { preset: '1', panes: ['b'] });
});

test('layoutForWidth: a null or unknown activeId falls back to the first open pane', () => {
  const state = { preset: '2col', panes: [null, 'x', 'y'] };
  assert.deepEqual(layoutForWidth(state, null, 300).panes, ['x']);
  assert.deepEqual(layoutForWidth(state, 'gone', 300).panes, ['x']);
  assert.deepEqual(layoutForWidth(state, undefined, 300).panes, ['x']);
});

test('layoutForWidth: no open panes gives an empty single-pane layout', () => {
  assert.deepEqual(layoutForWidth({ preset: '1', panes: [] }, null, 300), { preset: '1', panes: [] });
  assert.deepEqual(layoutForWidth({ preset: '2col', panes: [null, null] }, 'a', 300), { preset: '1', panes: [] });
});

test('layoutForWidth: never mutates its input and returns a new state when collapsing', () => {
  const state = { preset: '2x2', panes: ['a', 'b', 'c', 'd'] };
  const before = JSON.stringify(state);
  const out = layoutForWidth(state, 'b', 300);
  assert.equal(JSON.stringify(state), before);
  assert.notEqual(out, state);
  out.panes.push('z');
  assert.deepEqual(state.panes, ['a', 'b', 'c', 'd']);
});

test('DEFAULT_TEMPLATES holds the two built-in recipes', () => {
  assert.deepEqual(DEFAULT_TEMPLATES['driver + 2 workers'], { preset: '2x2', count: 3 });
  assert.deepEqual(DEFAULT_TEMPLATES['monitor grid'], { preset: '2x2', count: 4 });
});

test('saveTemplate returns a new object with the preset and pane count, leaving the input alone', () => {
  const templates = { a: { preset: '1', count: 1 } };
  const before = JSON.stringify(templates);
  const out = saveTemplate(templates, 'mine', { preset: '2col', panes: ['x', 'y'] });
  assert.deepEqual(out, { a: { preset: '1', count: 1 }, mine: { preset: '2col', count: 2 } });
  assert.equal(JSON.stringify(templates), before);
  assert.notEqual(out, templates);
  out.a.count = 9;
  assert.equal(templates.a.count, 1);
  assert.deepEqual(saveTemplate(templates, 'a', { preset: '1', panes: [] }).a, { preset: '1', count: 0 });
});

test('applyTemplate fills from the current panes first, then the open ids, and pads with null', () => {
  const state = { preset: '1', panes: ['p1'] };
  const out = applyTemplate(DEFAULT_TEMPLATES, 'driver + 2 workers', state, ['p1', 'o1', 'o2', 'o3']);
  assert.deepEqual(out, { preset: '2x2', panes: ['p1', 'o1', 'o2', null] });
});

test('applyTemplate pads a short supply with null placeholders up to the slot count', () => {
  const out = applyTemplate(DEFAULT_TEMPLATES, 'monitor grid', { preset: '1', panes: ['p1'] }, []);
  assert.deepEqual(out, { preset: '2x2', panes: ['p1', null, null, null] });
  const two = applyTemplate({ t: { preset: '2col', count: 2 } }, 't', { preset: '1', panes: [] }, []);
  assert.deepEqual(two, { preset: '2col', panes: [null, null] });
});

test('applyTemplate reuses already placed panes once and skips null slots in the state', () => {
  const state = { preset: '2col', panes: [null, 'a', 'a', 'b'] };
  const out = applyTemplate(DEFAULT_TEMPLATES, 'monitor grid', state, ['b', 'c']);
  assert.deepEqual(out.panes, ['a', 'b', 'c', null]);
});

test('applyTemplate caps a count above the preset slot count', () => {
  const out = applyTemplate({ big: { preset: '2col', count: 9 } }, 'big', { preset: '1', panes: ['a', 'b', 'c'] }, ['d']);
  assert.deepEqual(out, { preset: '2col', panes: ['a', 'b'] });
});

test('applyTemplate with an unknown name (or an inherited key) returns the state unchanged', () => {
  const state = { preset: '2col', panes: ['a', 'b'] };
  assert.equal(applyTemplate(DEFAULT_TEMPLATES, 'nope', state, ['c']), state);
  assert.equal(applyTemplate(DEFAULT_TEMPLATES, 'constructor', state, ['c']), state);
  assert.equal(applyTemplate({ bad: { preset: 'zzz', count: 1 } }, 'bad', state, ['c']), state);
});

test('applyTemplate ignores a template that is only inherited through the prototype', () => {
  const inherited = Object.create({ t: { preset: '2col', count: 2 } });
  const state = { preset: '1', panes: ['a'] };
  assert.equal(applyTemplate(inherited, 't', state, ['b']), state);
});

test('applyTemplate never mutates its inputs and returns fresh arrays', () => {
  const state = { preset: '1', panes: ['a'] };
  const open = ['b', 'c'];
  const templates = { t: { preset: '2col', count: 2 } };
  const snap = JSON.stringify([state, open, templates]);
  const out = applyTemplate(templates, 't', state, open);
  assert.equal(JSON.stringify([state, open, templates]), snap);
  out.panes.push('z');
  assert.deepEqual(state.panes, ['a']);
});

test('serializeTemplates and deserializeTemplates round-trip', () => {
  const templates = { a: { preset: '2col', count: 2 }, b: { preset: '2x2', count: 3 } };
  assert.deepEqual(deserializeTemplates(serializeTemplates(templates)), templates);
});

test('deserializeTemplates falls back to a copy of the defaults for malformed JSON or a non-object', () => {
  for (const bad of ['{nope', '', 'null', '5', '"s"', '[1,2]', undefined]) {
    const out = deserializeTemplates(bad);
    assert.deepEqual(out, DEFAULT_TEMPLATES);
    assert.notEqual(out, DEFAULT_TEMPLATES);
  }
  const out = deserializeTemplates('{nope');
  out['monitor grid'].count = 1;
  assert.equal(DEFAULT_TEMPLATES['monitor grid'].count, 4, 'the fallback is a deep copy');
});

test('deserializeTemplates drops entries with a bad preset or a bad count', () => {
  const json = JSON.stringify({
    ok: { preset: '2col', count: 2 }, badPreset: { preset: '3x3', count: 2 }, noPreset: { count: 1 },
    badCount: { preset: '1', count: 'x' }, negative: { preset: '1', count: -1 }, nul: null,
  });
  assert.deepEqual(deserializeTemplates(json), { ok: { preset: '2col', count: 2 } });
});

test('stripVisibleCount: 1 on a phone, 2 on a tablet or small laptop, 3 on a wide screen, boundaries at 760 and 1400', () => {
  assert.equal(stripVisibleCount(375), 1);
  assert.equal(stripVisibleCount(759), 1);
  assert.equal(stripVisibleCount(760), 2);
  assert.equal(stripVisibleCount(1399), 2);
  assert.equal(stripVisibleCount(1400), 3);
  assert.equal(stripVisibleCount(3000), 3);
  for (const bad of [undefined, null, NaN, 'wide', {}]) assert.equal(stripVisibleCount(bad), 1);
});

test('clampStripIndex: stays inside 0..(paneCount - visibleCount); non-numbers and a short strip give 0', () => {
  assert.equal(clampStripIndex(2, 5, 2), 2);
  assert.equal(clampStripIndex(9, 5, 2), 3);
  assert.equal(clampStripIndex(-4, 5, 2), 0);
  assert.equal(clampStripIndex(1.9, 5, 2), 1);
  for (const bad of [undefined, null, NaN, '2', {}]) assert.equal(clampStripIndex(bad, 5, 2), 0);
  assert.equal(clampStripIndex(3, 1, 2), 0, 'fewer panes than fit');
  assert.equal(clampStripIndex(3, 0, 1), 0, 'no panes');
  assert.equal(clampStripIndex(1, 'x', 2), 0, 'bad pane count');
});

test('stepStrip: moves by delta and stops at both ends', () => {
  assert.equal(stepStrip(0, 1, 4, 1), 1);
  assert.equal(stepStrip(3, 1, 4, 1), 3, 'last stays last');
  assert.equal(stepStrip(0, -1, 4, 1), 0, 'first stays first');
  assert.equal(stepStrip(1, 1, 4, 2), 2);
  assert.equal(stepStrip(2, 1, 4, 2), 2, 'two visible: the last index is paneCount - 2');
  assert.equal(stepStrip(1, 'x', 4, 1), 1, 'a bad delta does not move');
  assert.equal(stepStrip(99, -1, 4, 1), 2, 'an out-of-range index is clamped before stepping');
});

test('stripLabel: n / m for one visible pane, a range for several, empty with no panes', () => {
  assert.equal(stripLabel(0, 0, 1), '');
  assert.equal(stripLabel(0, 1, 1), '1 / 1');
  assert.equal(stripLabel(2, 4, 1), '3 / 4');
  assert.equal(stripLabel(0, 4, 2), '1-2 / 4');
  assert.equal(stripLabel(2, 4, 2), '3-4 / 4');
  assert.equal(stripLabel(0, 3, 3), '1-3 / 3');
  assert.equal(stripLabel(0, 1, 3), '1 / 1', 'one pane on a three-wide screen is not a range');
  assert.equal(stripLabel(9, 4, 2), '3-4 / 4', 'index is clamped');
});

test('stripButtons: canPrev and canNext at the first, middle and last position', () => {
  assert.deepEqual(stripButtons(0, 4, 1), { canPrev: false, canNext: true });
  assert.deepEqual(stripButtons(1, 4, 1), { canPrev: true, canNext: true });
  assert.deepEqual(stripButtons(3, 4, 1), { canPrev: true, canNext: false });
  assert.deepEqual(stripButtons(0, 2, 2), { canPrev: false, canNext: false }, 'everything fits');
  assert.deepEqual(stripButtons(0, 0, 1), { canPrev: false, canNext: false });
});
