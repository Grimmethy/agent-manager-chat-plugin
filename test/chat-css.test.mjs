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

test('.multi-chat-flex is a horizontal snap strip, not a wrapping vertical grid', () => {
  assert.ok(declares('.multi-chat-flex', 'flex-wrap'), 'missing flex-wrap');
  assert.ok(declares('.multi-chat-flex', 'overflow-x'), 'missing overflow-x');
  assert.ok(declares('.multi-chat-flex', 'scroll-snap-type'), 'missing scroll-snap-type');
  const block = blocksFor('.multi-chat-flex').join(' ');
  assert.match(block, /flex-wrap\s*:\s*nowrap/i, 'flex-wrap must be nowrap');
  assert.match(block, /overflow-x\s*:\s*auto/i, 'overflow-x must be auto');
  assert.match(block, /scroll-snap-type\s*:\s*x\s+mandatory/i, 'scroll-snap-type must be "x mandatory"');
});

test('.multi-chat-flex hides its scrollbar but still scrolls and snaps, so the buttons are the way to move', () => {
  const block = blocksFor('.multi-chat-flex').join(' ');
  assert.match(block, /scrollbar-width\s*:\s*none/i, 'Firefox-style scrollbar must be hidden');
  assert.match(blocksFor('.multi-chat-flex::-webkit-scrollbar').join(' '), /display\s*:\s*none/i, 'WebKit/Blink scrollbar must be hidden');
  assert.match(block, /overflow-x\s*:\s*auto/i, 'the strip must still scroll horizontally');
  assert.match(block, /scroll-snap-type\s*:\s*x\s+mandatory/i, 'the strip must still snap');
});

test('.multi-chat-flex sets --strip-visible from the width with the stripVisibleCount breakpoints', () => {
  const block = blocksFor('.multi-chat-flex').join(' ');
  assert.match(block, /(^|[;\s])--strip-visible\s*:\s*2/i, 'default of two visible panes');
  assert.match(css, /@media\s*\(\s*max-width\s*:\s*759px\s*\)\s*\{\s*\.multi-chat-flex\s*\{\s*--strip-visible\s*:\s*1/i, 'one pane under 760px');
  assert.match(css, /@media\s*\(\s*min-width\s*:\s*1400px\s*\)\s*\{\s*\.multi-chat-flex\s*\{\s*--strip-visible\s*:\s*3/i, 'three panes from 1400px');
});

test('.multi-chat-pane snaps to the start of the strip', () => {
  assert.ok(declares('.multi-chat-pane', 'scroll-snap-align'), 'missing scroll-snap-align');
  const block = blocksFor('.multi-chat-pane').join(' ');
  assert.match(block, /scroll-snap-align\s*:\s*start/i, 'scroll-snap-align must be start');
});

test('the strip navigation: a hidden-aware inline row and 44px touch buttons', () => {
  const nav = blocksFor('.multi-chat-strip-nav').join(' ');
  assert.match(nav, /display\s*:\s*inline-flex/i);
  assert.ok(blocksFor('.multi-chat-strip-nav[hidden]').join(' ').match(/display\s*:\s*none/i), 'the hidden attribute must hide it');
  const coarse = mediaBlocksFor('(pointer: coarse)', '.multi-chat-strip-btn').join(' ');
  assert.match(coarse, /min-width\s*:\s*44px/);
  assert.match(coarse, /min-height\s*:\s*44px/);
});

test('the page is one full-height column: #chat-panel fills the dynamic viewport and the strip host shrinks to fit', () => {
  const panel = blocksFor('#chat-panel').join(' ');
  assert.match(panel, /display:\s*flex/);
  assert.match(panel, /flex-direction:\s*column/);
  assert.match(panel, /height:\s*100dvh/);
  assert.match(panel, /height:\s*100vh/, 'a 100vh fallback stays for browsers without dvh');
  assert.match(blocksFor('.multi-chat-host').join(' '), /min-height:\s*0/);
  assert.match(blocksFor('.multi-chat-toolbar').join(' '), /flex:\s*0 0 auto/);
});

test('under 760px only the Layout control (presets and templates) is hidden; the toolbar itself stays', () => {
  const hide = css.match(/@media \(max-width: 759px\)\s*\{\s*([^{}]*)\{\s*display:\s*none/);
  assert.ok(hide, 'no phone rule hides anything');
  assert.deepEqual(hide[1].split(',').map((x) => x.trim()).sort(), ['.multi-chat-layout']);
  assert.equal(blocksFor('.multi-chat-toolbar').some((b) => /display:\s*none/.test(b)), false);
});

test('a pane header is one non-wrapping line with the title on the left and compact buttons, and the empty switcher slot takes no room', () => {
  const header = blocksFor('.chat-header').join(' ');
  assert.match(header, /flex-wrap:\s*nowrap/);
  assert.match(header, /overflow-x:\s*auto/, 'a very narrow pane scrolls its header instead of wrapping to a second row');
  const title = blocksFor('.chat-pane-title').join(' ');
  assert.match(title, /flex:\s*1 1 0/);
  assert.match(title, /text-overflow:\s*ellipsis/);
  assert.match(title, /white-space:\s*nowrap/);
  assert.match(blocksFor('.chat-switcher-slot:empty').join(' '), /display:\s*none/);
  assert.match(blocksFor('.chat-switcher-slot:not(:empty) ~ .chat-pane-title').join(' '), /display:\s*none/);
  const buttons = blocksFor('.chat-header button').join(' ');
  assert.match(buttons, /height:\s*30px/);
  assert.ok(blocksFor('.multi-chat-close').length > 0, 'the close button shares the header button size');
  assert.ok(mediaBlocksFor('(pointer: coarse)', '.multi-chat-close').join(' ').match(/min-height:\s*44px/), 'close keeps a 44px touch target');
});

test('the strip fills the host and a pane body is a column, so the transcript gets the height the old two header rows wasted', () => {
  const strip = blocksFor('.multi-chat-flex').join(' ');
  assert.match(strip, /flex:\s*1 1 0/);
  assert.match(strip, /min-height:\s*0/);
  const body = blocksFor('.multi-chat-pane-body').join(' ');
  assert.match(body, /display:\s*flex/);
  assert.match(body, /flex-direction:\s*column/);
});

test('the Layout control: one button at the end of the bar with a popover for the presets and the templates, 44px on touch', () => {
  assert.match(blocksFor('.multi-chat-layout').join(' '), /position:\s*relative/);
  const menu = blocksFor('.multi-chat-layout-menu').join(' ');
  assert.match(menu, /position:\s*absolute/);
  assert.match(menu, /z-index:\s*100/);
  assert.match(blocksFor('.multi-chat-layout-menu[hidden]').join(' '), /display:\s*none/);
  assert.match(blocksFor('.multi-chat-preset-btn[aria-pressed="true"]').join(' '), /background:\s*var\(--accent\)/);
  for (const selector of ['.multi-chat-layout-btn', '.multi-chat-preset-btn']) {
    assert.match(mediaBlocksFor('(pointer: coarse)', selector).join(' '), /min-height:\s*44px/, selector + ' needs a 44px touch target');
  }
});

test('the top bar: open chats are a scrollable row of buttons with a highlighted active one, and every bar control shares one size', () => {
  const tabs = blocksFor('.multi-chat-tabs').join(' ');
  assert.match(tabs, /display:\s*flex/);
  assert.match(tabs, /overflow-x:\s*auto/);
  assert.match(tabs, /scrollbar-width:\s*none/);
  assert.ok(blocksFor('.multi-chat-tabs::-webkit-scrollbar').join(' ').match(/display:\s*none/));
  assert.match(blocksFor('.multi-chat-tab').join(' '), /text-overflow:\s*ellipsis/);
  assert.match(blocksFor('.multi-chat-tab[aria-selected="true"]').join(' '), /background:\s*var\(--accent\)/);
  const control = blocksFor('.multi-chat-toolbar button').join(' ');
  assert.match(control, /height:\s*30px/);
  assert.match(control, /border-radius:\s*6px/);
  assert.ok(blocksFor('.multi-chat-toolbar select').length > 0, 'the templates select shares the bar control size');
  for (const selector of ['.multi-chat-tab', '.multi-chat-new-btn', '.multi-chat-switcher .chat-switcher-btn']) {
    assert.match(mediaBlocksFor('(pointer: coarse)', selector).join(' '), /min-height:\s*44px/, selector + ' needs a 44px touch target');
  }
});

test('the All chats list is anchored to the whole bar (not its mid-bar button) and kept inside it, so it never leaves a narrow frame', () => {
  assert.match(blocksFor('.multi-chat-toolbar').join(' '), /position:\s*relative/);
  assert.match(blocksFor('.multi-chat-switcher').join(' '), /position:\s*static/);
  const menu = blocksFor('.multi-chat-switcher .chat-switcher-menu').join(' ');
  assert.match(menu, /right:\s*8px/);
  assert.match(menu, /left:\s*auto/);
  assert.match(menu, /max-width:\s*calc\(100% - 16px\)/);
});

test('on a phone the open-chat buttons take a row of their own above the other controls', () => {
  assert.match(css, /@media \(max-width: 759px\)\s*\{\s*\.multi-chat-tabs\s*\{[^}]*flex:\s*1 0 100%[^}]*order:\s*-1/);
});

test('a pane under 480px wide (the dashboard sidebar) drops the repeated provider label and tightens its header so the close button still fits', () => {
  assert.match(blocksFor('.multi-chat-pane').join(' '), /container-type:\s*inline-size/);
  const m = css.match(/@container\s*\(\s*max-width\s*:\s*480px\s*\)\s*\{([\s\S]*?)\n\}/);
  assert.ok(m, 'no @container (max-width: 480px) rule');
  assert.match(m[1], /\.chat-header \.chat-provider-label\s*\{\s*display:\s*none/);
  assert.match(m[1], /\.chat-header\s*\{[^}]*gap:\s*4px/);
  assert.match(m[1], /\.chat-header button, \.multi-chat-close\s*\{[^}]*padding:\s*0 8px/);
});
