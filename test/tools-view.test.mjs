import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToolsHtml } from '../static/tools-view.js';

test('nothing to show gives an empty string (no tools key, empty list, junk)', () => {
  for (const v of [undefined, null, [], 'x', 7, [null, 3]]) assert.equal(renderToolsHtml(v), '');
});

test('one collapsed details with a count and a row per tool: name, arguments, outcome', () => {
  const html = renderToolsHtml([
    { name: 'run_bash', args: '{"command": "ls"}', ok: true, summary: '4 chars returned' },
    { name: 'read_file', args: '{"path": "/x"}', ok: true, summary: '10 chars returned' },
  ]);
  assert.match(html, /^<details class="chat-tools"><summary>Ran 2 tools<\/summary>/);
  assert.equal((html.match(/class="chat-tool-row"/g) || []).length, 2);
  assert.match(html, /<span class="chat-tool-name">run_bash<\/span>/);
  assert.match(html, /<span class="chat-tool-outcome">4 chars returned<\/span>/);
});

test('a single tool reads "Ran 1 tool" and a failed one is marked and counted', () => {
  assert.match(renderToolsHtml([{ name: 'a', args: '', ok: true, summary: 's' }]), /<summary>Ran 1 tool<\/summary>/);
  const html = renderToolsHtml([{ name: 'a', args: '', ok: true, summary: 's' }, { name: 'b', args: '', ok: false, summary: 'boom' }]);
  assert.match(html, /<summary>Ran 2 tools \(1 failed\)<\/summary>/);
  assert.match(html, /class="chat-tool-row chat-tool-bad"/);
});

test('every field is escaped', () => {
  const html = renderToolsHtml([{ name: '<img src=x onerror=1>', args: '"><script>alert(1)</script>', ok: true, summary: "a&b'c" }]);
  assert.doesNotMatch(html, /<script|<img/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /a&amp;b&#39;c/);
});

test('chat.js imports the renderer and puts it between the message text and the command block and template cards', () => {
  const src = readFileSync(fileURLToPath(new URL('../static/chat.js', import.meta.url)), 'utf8');
  assert.match(src, /import \{ renderToolsHtml \} from "\.\/tools-view\.js";/);
  assert.match(src, /<div class="chat-msg-text">\$\{body\}<\/div>\$\{renderToolsHtml\(t\.tools\)\}\$\{renderCommandsHtml\(t\)\}\$\{cards\}/);
});

test('the tools block is styled with the existing palette tokens', () => {
  const css = readFileSync(fileURLToPath(new URL('../static/chat.css', import.meta.url)), 'utf8');
  for (const sel of ['.chat-tools', '.chat-tool-row', '.chat-tool-name', '.chat-tool-args', '.chat-tool-bad']) assert.ok(css.includes(sel), sel + ' missing');
});
