// Unit tests for buildPreemptText in static/chat.js -- the pure function that
// turns a preempt SSE event's rows into the operator-visible chat text.
// static/chat.js is a browser script (not a module), so we lift the function's
// source out of the file and evaluate it in a clean vm context.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const src = readFileSync(new URL('../static/chat.js', import.meta.url), 'utf8');
const start = src.indexOf('function buildPreemptText');
assert.notEqual(start, -1, 'buildPreemptText must be defined in static/chat.js');
const braceStart = src.indexOf('{', start);
let depth = 0;
let end = -1;
for (let j = braceStart; j < src.length; j++) {
  if (src[j] === '{') depth++;
  else if (src[j] === '}') {
    depth--;
    if (depth === 0) { end = j; break; }
  }
}
assert.notEqual(end, -1, 'buildPreemptText body must close');
const buildPreemptText = vm.runInNewContext(src.slice(start, end + 1) + '\n;buildPreemptText', {});

const killedRow = { lane: 'worker-3090', action: 'killed', taskId: 't1', phase: 'inference', ageSeconds: 5 };
const sparedRow = { lane: 'reviewer', action: 'spared', taskId: 't2', phase: 'inference', ageSeconds: 300 };
const busyRow = { lane: 'worker-3090', action: 'busy', taskId: 't9', phase: 'decode', ageSeconds: null };

test('killed-only rows keep the existing freed phrasing and show no busy text', () => {
  const text = buildPreemptText([killedRow]);
  assert.equal(text, '⚡ freed worker-3090\n\n');
  assert.ok(!text.includes('busy:'));
});

test('spared-only rows keep the existing spared phrasing and show no busy text', () => {
  const text = buildPreemptText([sparedRow]);
  assert.equal(text, '⚡ spared reviewer (5m in)\n\n');
  assert.ok(!text.includes('busy:'));
});

test('busy-only rows render the holder the chat is queued behind', () => {
  const text = buildPreemptText([busyRow]);
  assert.equal(text, '⚡ busy: worker-3090 t9 (decode) - your turn is queued behind it\n\n');
});

test('mixed rows: freed, spared and busy joined by " · " with prefix and two newlines', () => {
  const text = buildPreemptText([killedRow, sparedRow, busyRow]);
  assert.ok(text.startsWith('⚡ '), 'lightning-bolt prefix kept');
  assert.ok(text.endsWith('\n\n'), 'two trailing newlines kept');
  assert.ok(text.includes('freed worker-3090'));
  assert.ok(text.includes('spared reviewer (5m in)'));
  assert.ok(text.includes('busy: worker-3090 t9 (decode) - your turn is queued behind it'));
  const bits = text.replace(/^⚡ /, '').replace(/\n\n$/, '').split(' · ');
  assert.equal(bits.length, 3);
});

test('unknown actions are ignored and produce no text', () => {
  assert.equal(buildPreemptText([{ lane: 'x', action: 'mystery', taskId: 't', phase: 'p', ageSeconds: 1 }]), '');
  const text = buildPreemptText([killedRow, { lane: 'x', action: 'mystery', taskId: 't', phase: 'p', ageSeconds: 1 }]);
  assert.equal(text, '⚡ freed worker-3090\n\n');
});
