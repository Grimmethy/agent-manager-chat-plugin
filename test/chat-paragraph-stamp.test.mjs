// Automated test for the per-paragraph arrival stamps in static/chat.js.
// Runs under `node --test` with no DevTools or manual step. static/chat.js is a
// browser script (not a module), so chatNoteParagraphs, chatParagraphsBody and
// chatStampOf are lifted out of its source and evaluated in a vm context with a
// minimal injected stub (fake clock, escapeHtml, renderMarkdown).
//
// Assumed contract (from the task text):
//   chatNoteParagraphs(msg, text) -- records msg.paragraphs[] = {text, arrivedAt}
//     from the streamed text so far; existing paragraphs keep their arrivedAt.
//     The arrival time comes from the (stubbed) clock, i.e. new Date().
//   chatStampOf(iso) -- 'HH:MM:SS' for an ISO timestamp.
//   chatParagraphsBody(msg, isAssistant) -- HTML string, one stamp per paragraph;
//     a message without paragraphs falls back to msg.timestamp.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const src = readFileSync(new URL('../static/chat.js', import.meta.url), 'utf8');

function extractFunction(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start === -1) {
    throw new Error(name + ' not found in static/chat.js (sibling HUB0071 4/5 not landed?)');
  }
  const braceStart = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let j = braceStart; j < source.length; j++) {
    if (source[j] === '{') depth++;
    else if (source[j] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, j + 1);
    }
  }
  throw new Error(name + ' body does not close in static/chat.js');
}

const NAMES = ['chatStampOf', 'chatNoteParagraphs', 'chatParagraphsBody'];
const fnSource = NAMES.map((n) => extractFunction(src, n)).join('\n');

// Fake clock: tests set clock.now (ms since epoch, UTC) -- the real clock is never read.
const clock = { now: Date.UTC(2026, 0, 1, 10, 0, 0) };
class FakeDate extends Date {
  constructor(...args) { super(...(args.length ? args : [clock.now])); }
  static now() { return clock.now; }
}
const ctx = vm.createContext({
  Date: FakeDate,
  escapeHtml: (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
  renderMarkdown: (s) => String(s),
});
vm.runInContext(fnSource, ctx);
const { chatNoteParagraphs, chatParagraphsBody, chatStampOf } = vm.runInContext(
  '({chatNoteParagraphs, chatParagraphsBody, chatStampOf})', ctx);

const at = (h, m, s) => Date.UTC(2026, 0, 1, h, m, s);
const stampsIn = (html) => [...String(html).matchAll(/\b(\d{2}:\d{2}:\d{2})\b/g)].map((x) => x[1]);

test('three paragraphs with distinct arrivedAt render three distinct stamps in order', () => {
  const msg = {
    role: 'assistant',
    text: 'one\n\ntwo\n\nthree',
    paragraphs: [
      { text: 'one', arrivedAt: new Date(at(10, 0, 1)).toISOString() },
      { text: 'two', arrivedAt: new Date(at(10, 5, 2)).toISOString() },
      { text: 'three', arrivedAt: new Date(at(10, 10, 3)).toISOString() },
    ],
  };
  const stamps = stampsIn(chatParagraphsBody(msg, true));
  assert.equal(stamps.length, 3);
  assert.equal(new Set(stamps).size, 3);
  assert.deepEqual(stamps, msg.paragraphs.map((p) => chatStampOf(p.arrivedAt)));
});

test('chunk-driven paragraphs keep their first-arrival stamps', () => {
  const msg = { role: 'assistant', text: '' };
  clock.now = at(10, 0, 1);
  msg.text = 'first';
  chatNoteParagraphs(msg, msg.text);
  clock.now = at(10, 0, 9);
  msg.text = 'first para grows\n\nsecond';
  chatNoteParagraphs(msg, msg.text);
  clock.now = at(10, 0, 20);
  msg.text = 'first para grows\n\nsecond grows too';
  chatNoteParagraphs(msg, msg.text);
  assert.equal(msg.paragraphs.length, 2);
  assert.equal(chatStampOf(msg.paragraphs[0].arrivedAt), '10:00:01');
  assert.equal(chatStampOf(msg.paragraphs[1].arrivedAt), '10:00:09');
  assert.deepEqual(stampsIn(chatParagraphsBody(msg, true)), ['10:00:01', '10:00:09']);
});

test('a message without paragraphs falls back to the legacy message-level timestamp', () => {
  const msg = { role: 'assistant', text: 'a\n\nb', timestamp: '2026-01-01T09:08:07Z' };
  const stamps = stampsIn(chatParagraphsBody(msg, true));
  assert.ok(stamps.length >= 1);
  assert.ok(stamps.every((s) => s === '09:08:07'));
});
