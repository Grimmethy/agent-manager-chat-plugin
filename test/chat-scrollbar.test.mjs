// Scrollbars (owner request 2026-11-02): the Chat panel's browser-default light grey bars clashed with the dark theme. Same look as the host dashboard:
// slim, fully rounded, blue. Pure text checks on static/chat.css.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const raw = readFileSync(fileURLToPath(new URL('../static/chat.css', import.meta.url)), 'utf8');
const css = raw.replace(/\/\*[\s\S]*?\*\//g, '');

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\:]/g, '\\$&');
  const m = new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  assert.ok(m, `rule not found: ${selector}`);
  return m[1];
}
const declaration = (body, prop) => {
  const m = new RegExp(`(?:^|;|\\s)${prop}:\\s*([^;]+);`).exec(body + ';');
  return m ? m[1].trim() : null;
};

test('the scrollbar is 12px wide and tall', () => {
  const b = rule('::-webkit-scrollbar');
  assert.equal(declaration(b, 'width'), '12px');
  assert.equal(declaration(b, 'height'), '12px');
});

test('the track is a faint blue, fully rounded', () => {
  const b = rule('::-webkit-scrollbar-track');
  assert.equal(declaration(b, 'background'), 'rgba(91, 157, 255, .06)');
  assert.equal(declaration(b, 'border-radius'), '999px');
});

test('the thumb is a rounded pill in the strong blue, inset by a transparent border, and turns the accent blue on hover', () => {
  const t = rule('::-webkit-scrollbar-thumb');
  assert.equal(declaration(t, 'background'), 'var(--border-strong)');
  assert.equal(declaration(t, 'background-clip'), 'padding-box');
  assert.equal(declaration(t, 'border'), '3px solid transparent');
  assert.equal(declaration(t, 'border-radius'), '999px');
  const h = rule('::-webkit-scrollbar-thumb:hover');
  assert.equal(declaration(h, 'background'), 'var(--accent)');
  assert.equal(declaration(h, 'background-clip'), 'padding-box');
  assert.equal(declaration(rule('::-webkit-scrollbar-corner'), 'background'), 'transparent');
});

test('browsers without the webkit pseudo-elements get the thin standard scrollbar in the same colours', () => {
  const m = /@supports not selector\(::-webkit-scrollbar\) \{ \* \{ ([^}]*)\} \}/.exec(css);
  assert.ok(m, 'fallback block');
  assert.equal(declaration(m[1], 'scrollbar-width'), 'thin');
  assert.equal(declaration(m[1], 'scrollbar-color'), 'var(--border-strong) rgba(91, 157, 255, .06)');
});

test('--border-strong is declared once, in :root, and the thumb and hover colours reach 3:1 against the page and the cards', () => {
  assert.equal((css.match(/--border-strong:\s*#/g) || []).length, 1);
  const root = /:root \{([^}]*)\}/.exec(css)[1];
  const v = (name) => new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(root)[1];
  const lum = (hex) => {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  for (const bg of ['bg', 'panel']) {
    assert.ok(ratio(v('border-strong'), v(bg)) >= 3, `thumb on ${bg}`);
    assert.ok(ratio(v('accent'), v(bg)) >= 3, `hover on ${bg}`);
  }
});

test('the places that hide their scrollbar on purpose still do', () => {
  assert.match(css, /\.chat-header \{[^}]*scrollbar-width: none;/);
  assert.match(css, /\.multi-chat-tabs \{[^}]*scrollbar-width: none;/);
  assert.match(css, /\.multi-chat-flex::-webkit-scrollbar \{ display: none; \}/);
  assert.match(css, /\.multi-chat-tabs::-webkit-scrollbar \{ display: none; \}/);
});

test('the old default look is gone: no scrollbar-width auto, no square thumb', () => {
  assert.doesNotMatch(css, /scrollbar-width:\s*auto/);
  assert.doesNotMatch(rule('::-webkit-scrollbar-thumb'), /border-radius:\s*0/);
});
