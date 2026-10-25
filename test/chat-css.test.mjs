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

// The body of a given @media rule, extracted with balanced braces so nested rule
// blocks inside it are kept whole.
function mediaBody(query) {
  const start = css.indexOf('@media ' + query);
  if (start === -1) throw new Error('no @media ' + query + ' block');
  const open = css.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}') {
      depth--;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  throw new Error('unbalanced @media ' + query + ' block');
}

// Every declaration block INSIDE `@media <query>` whose selector list mentions `selector`.
function mediaBlocksFor(query, selector) {
  const out = [];
  for (const m of mediaBody(query).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sels = m[1].split(',').map((s) => s.trim());
    if (sels.includes(selector)) out.push(m[2]);
  }
  return out;
}

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

test('.chat-switcher-slot claims a 12rem flex basis and keeps its relative position', () => {
  const block = blocksFor('.chat-switcher-slot').join(' ');
  assert.match(block, /flex\s*:\s*1\s+1\s+12rem/, 'slot has no flex-basis of 12rem (name button gets squeezed on narrow screens)');
  assert.match(block, /min-width\s*:\s*0/);
  assert.match(block, /position\s*:\s*relative/);
});

test('.chat-switcher-title wraps to two clamped lines instead of one clipped line', () => {
  const block = blocksFor('.chat-switcher-title').join(' ');
  assert.match(block, /white-space\s*:\s*normal/);
  assert.match(block, /-webkit-line-clamp\s*:\s*2/);
  assert.match(block, /-webkit-box-orient\s*:\s*vertical/);
  assert.match(block, /overflow\s*:\s*hidden/);
  assert.match(block, /overflow-wrap\s*:\s*anywhere/);
});

test('the closed .chat-switcher-btn button keeps its single-line ellipsis behaviour', () => {
  const block = blocksFor('.chat-switcher-btn').join(' ');
  assert.match(block, /white-space\s*:\s*nowrap/);
  assert.match(block, /text-overflow\s*:\s*ellipsis/);
});

test('touch (pointer: coarse) gets 44px touch targets and a 20px chime checkbox', () => {
  const coarse = '(pointer: coarse)';
  for (const selector of [
    '.chat-switcher-close',
    '.chat-header button',
    '.chat-send-row button',
    '.chat-switcher-new',
    '.chat-chime-setting',
  ]) {
    const block = mediaBlocksFor(coarse, selector).join(' ');
    assert.match(block, /min-height\s*:\s*44px/, `${selector} has no 44px min-height under ${coarse}`);
    assert.match(block, /min-width\s*:\s*44px/, `${selector} has no 44px min-width under ${coarse}`);
  }
  const checkbox = mediaBlocksFor(coarse, '.chat-chime-setting input').join(' ');
  assert.match(checkbox, /width\s*:\s*20px/);
  assert.match(checkbox, /height\s*:\s*20px/);
});
