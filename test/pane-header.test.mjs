// The per-pane header (owner 2026-10-24): one line, title left, buttons right, close last. Source-text checks on the two
// browser files, as there is no DOM in node: static/chat.js builds the header, static/multi-chat.js adds the close button to it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (name) => readFileSync(fileURLToPath(new URL('../static/' + name, import.meta.url)), 'utf8');
const chatJs = read('chat.js');
const multiJs = read('multi-chat.js');

test('ChatPane builds a title element in its header and fills it from the session title', () => {
  assert.match(chatJs, /<span class="chat-pane-title"><\/span>/);
  assert.match(chatJs, /this\.titleEl = q\('\.chat-pane-title'\)/);
  assert.match(chatJs, /\(session \? fullTitle\(session\.title\) : ''\)/, 'the session title stays the fallback');
});

test('paneSlot puts the close button into the pane\'s own .chat-header instead of a header row of its own', () => {
  assert.doesNotMatch(multiJs, /multi-chat-pane-head/);
  assert.match(multiJs, /body\.querySelector\('\.chat-header'\)/);
  assert.match(multiJs, /header\.appendChild\(close\)/);
});

test('the pane header shows the chat\'s real title: setTitle on ChatPane, filled from the sessions rows by renderTabs', () => {
  assert.match(chatJs, /setTitle\(text\) \{/);
  assert.match(chatJs, /this\.titleText && this\.titleFor === this\.sessionId \? this\.titleText/);
  assert.match(multiJs, /for \(const p of livePanes\) if \(p\.sessionId && typeof titles\[p\.sessionId\] === 'string'\) p\.setTitle\(titles\[p\.sessionId\]\)/);
});
