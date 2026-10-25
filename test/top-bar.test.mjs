// The top bar wiring (owner 2026-10-24): source-text checks, as there is no DOM in node. static/multi-chat.js builds the open-chat
// buttons, a "+" button and hands the session switcher an 'All chats' label; static/chat.js lets SessionSwitcher take that label.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (name) => readFileSync(fileURLToPath(new URL('../static/' + name, import.meta.url)), 'utf8');
const multiJs = read('multi-chat.js');
const chatJs = read('chat.js');

test('the bar draws one button per open chat from chatTabs, selects a chat on click and has a + new-chat button', () => {
  assert.match(multiJs, /for \(const tab of chatTabs\(state, titles, activeId\)\)/);
  assert.match(multiJs, /btn\.onclick = \(\) => selectChat\(tab\.id\)/);
  assert.match(multiJs, /newTabBtn\.onclick = \(\) => openNewChat\(\)/);
  assert.match(multiJs, /aria-selected/);
});

test('the open-chat buttons are redrawn when the layout, the active chat or a chat title changes', () => {
  assert.match(multiJs, /onSessionChange: \(\) => refreshSwitcher\(\)/);
  assert.match(multiJs, /switcher\.refresh\(\)\.then\(renderTabs/);
  assert.match(multiJs, /activeId = open\.id; renderTabs\(\)/);
  assert.match(multiJs, /activeId = landed/);
});

test('the session switcher is the "All chats" list: SessionSwitcher takes a fixed button label', () => {
  assert.match(multiJs, /buttonLabel: 'All chats'/);
  assert.match(chatJs, /buttonLabel \}\) \{/);
  assert.match(chatJs, /this\.buttonLabel \|\| \(row \? fullTitle\(row\.title\) : 'Chats'\)/);
});

test('+ new chat renders the whole strip (so every open chat stays mounted) with the new chat in the claimed slot', () => {
  const fn = multiJs.slice(multiJs.indexOf('function openNewChat()'), multiJs.indexOf('// Re-render only when crossing'));
  assert.match(fn, /pendingNewAt = idx === -1 \? state\.panes\.length - 1 : idx/);
  assert.match(fn, /render\(\);/);
  assert.doesNotMatch(fn, /Open chat/, 'openNewChat no longer builds "Open chat…" placeholders for the other panes');
  assert.match(multiJs, /id === null && i === pendingNewAt/);
});
