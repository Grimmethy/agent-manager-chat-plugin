// Unit tests for chatSegmentsWithStamps in static/chat.js -- the pure function that
// splits assistant text into paragraphs and pairs each with its arrival-time stamp.
// static/chat.js is a browser script (not a module), so we lift the function's
// source out of the file and evaluate it in a clean vm context.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const src = readFileSync(new URL('../static/chat.js', import.meta.url), 'utf8');
const start = src.indexOf('function chatSegmentsWithStamps');
assert.notEqual(start, -1, 'chatSegmentsWithStamps must be defined in static/chat.js');
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
assert.notEqual(end, -1, 'chatSegmentsWithStamps body must close');
const helperStart = src.indexOf('function chatStampText');
assert.notEqual(helperStart, -1, 'chatStampText must be defined in static/chat.js');
const helperBraceStart = src.indexOf('{', helperStart);
let helperDepth = 0;
let helperEnd = -1;
for (let j = helperBraceStart; j < src.length; j++) {
  if (src[j] === '{') helperDepth++;
  else if (src[j] === '}') {
    helperDepth--;
    if (helperDepth === 0) { helperEnd = j; break; }
  }
}
assert.notEqual(helperEnd, -1, 'chatStampText body must close');
const chatStampText = vm.runInNewContext(src.slice(helperStart, helperEnd + 1) + '\n;chatStampText', {});
const chatSegmentsWithStamps = vm.runInNewContext(
  src.slice(start, end + 1) + '\n;chatSegmentsWithStamps',
  { chatStampText }
);

test('three paragraphs with distinct paragraph_times each get their own stamp', () => {
  const t = {
    text: 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.',
    paragraph_times: ['2025-01-01T10:00:00Z', '2025-01-01T10:05:00Z', '2025-01-01T10:10:00Z'],
  };
  const segments = chatSegmentsWithStamps(t);
  assert.equal(segments.length, 3);
  assert.equal(segments[0].text, 'First paragraph.');
  assert.equal(segments[0].stamp, '10:00:00');
  assert.equal(segments[1].text, 'Second paragraph.');
  assert.equal(segments[1].stamp, '10:05:00');
  assert.equal(segments[2].text, 'Third paragraph.');
  assert.equal(segments[2].stamp, '10:10:00');
});

test('no paragraph_times falls back to timestamp for every paragraph', () => {
  const t = {
    text: 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.',
    timestamp: '2025-01-01T09:00:00Z',
  };
  const segments = chatSegmentsWithStamps(t);
  assert.equal(segments.length, 3);
  assert.equal(segments[0].stamp, '09:00:00');
  assert.equal(segments[1].stamp, '09:00:00');
  assert.equal(segments[2].stamp, '09:00:00');
});

test('empty text returns one segment with empty text', () => {
  const segments = chatSegmentsWithStamps({ text: '' });
  assert.equal(segments.length, 1);
  assert.equal(segments[0].text, '');
  assert.equal(segments[0].stamp, '');
});

test('single paragraph returns one segment', () => {
  const t = {
    text: 'Only paragraph.',
    paragraph_times: ['2025-01-01T12:00:00Z'],
  };
  const segments = chatSegmentsWithStamps(t);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].text, 'Only paragraph.');
  assert.equal(segments[0].stamp, '12:00:00');
});

test('invalid input returns empty array', () => {
  assert.equal(chatSegmentsWithStamps(null).length, 0);
  assert.equal(chatSegmentsWithStamps({}).length, 0);
  assert.equal(chatSegmentsWithStamps({ text: 123 }).length, 0);
});
