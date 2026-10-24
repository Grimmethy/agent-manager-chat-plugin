// The switcher filter box and the layout toolbar's Templates select (and its name box) must use the dark theme like the
// textarea and buttons; a bare browser default renders white on the dark page. Pure text checks on static/chat.css.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Comments are stripped first so a /* note */ above a rule does not become part of its first selector.
const css = readFileSync(fileURLToPath(new URL('../static/chat.css', import.meta.url)), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

// Every declaration block whose selector list mentions `selector`.
function blocksFor(selector) {
  const out = [];
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sels = m[1].split(',').map((s) => s.trim());
    if (sels.includes(selector)) out.push(m[2]);
  }
  return out;
}
const declares = (selector, prop) => blocksFor(selector).some((b) => new RegExp('(^|[;\\s])' + prop + '\\s*:', 'i').test(b));

for (const selector of ['.chat-switcher-filter', '.multi-chat-template-select', '.multi-chat-template-name']) {
  test(`${selector} sets its own dark background and text colour`, () => {
    assert.ok(declares(selector, 'background'), `${selector} has no background rule (renders white)`);
    assert.ok(declares(selector, 'color'), `${selector} has no text colour rule`);
  });
}

test('the new rules use the existing theme variables, not new colours', () => {
  const block = blocksFor('.multi-chat-template-select').join(' ');
  assert.match(block, /var\(--panel\)/);
  assert.match(block, /var\(--text\)/);
  assert.match(block, /var\(--border\)/);
});
