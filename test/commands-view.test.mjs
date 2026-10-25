import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderCommandsHtml } from '../static/commands-view.js';

const proposal = (extra = {}) => ({ proposalId: 'cmd-1', repo: '/media/model-cache/github/inventory-plugin', argv: ['python3', '-m', 'inventory', 'sales', 'email'], why: 'ingest the newest eBay sale emails', status: 'pending', decidedAt: null, ...extra });
const run = (extra = {}) => ({ proposalId: 'cmd-1', argv: ['python3', '-m', 'inventory', 'sales', 'email'], repo: '/r', confirmedAt: '2026-10-25T10:00:00+00:00', exitCode: 0, ok: true, stdout: 'ingested 2 sales', stderr: '', durationMs: 2500, ...extra });
const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');

test('nothing to show gives an empty string', () => {
  for (const v of [undefined, null, {}, { commandProposals: [] }, { commandProposals: [null, {}], commandExecutions: 'x' }, 'x', 7]) assert.equal(renderCommandsHtml(v), '');
});

test('a pending proposal shows the exact command, repo and why with Confirm and Deny buttons and says nothing has run', () => {
  const html = renderCommandsHtml({ commandProposals: [proposal()] });
  assert.match(html, /<div class="cmd-card cmd-pending" data-proposal="cmd-1" data-status="pending">/);
  assert.match(html, /<code class="cmd-argv">python3 -m inventory sales email<\/code>/);
  assert.match(html, /in \/media\/model-cache\/github\/inventory-plugin/);
  assert.match(html, /ingest the newest eBay sale emails/);
  assert.match(html, /nothing has run/);
  assert.equal((html.match(/class="action cmd-confirm"/g) || []).length, 1);
  assert.equal((html.match(/class="secondary cmd-deny"/g) || []).length, 1);
});

test('a decided proposal has no buttons: confirmed, denied and ran each say so', () => {
  for (const [status, text] of [['confirmed', /Confirmed/], ['denied', /Denied -- it was not run/], ['ran', /Ran\./]]) {
    const html = renderCommandsHtml({ commandProposals: [proposal({ status })] });
    assert.match(html, text);
    assert.doesNotMatch(html, /\bcmd-(confirm|deny)\b/);
    assert.match(html, new RegExp(`data-status="${status}"`));
  }
  assert.match(renderCommandsHtml({ commandProposals: [proposal({ status: 'weird' })] }), /data-status="pending"/, 'an unknown status is treated as pending, never as run');
});

test('an execution shows the exit code, duration and output; a failure is marked and a refusal says it did not run', () => {
  const ok = renderCommandsHtml({ commandExecutions: [run()] });
  assert.match(ok, /<summary>Exit code 0 in 2\.5 s: python3 -m inventory sales email<\/summary>/);
  assert.match(ok, /<pre class="cmd-out">ingested 2 sales<\/pre>/);
  assert.doesNotMatch(ok, /cmd-run-bad|cmd-err/);
  const bad = renderCommandsHtml({ commandExecutions: [run({ exitCode: 3, ok: false, stdout: '', stderr: 'boom' })] });
  assert.match(bad, /cmd-run cmd-run-bad/);
  assert.match(bad, /Exit code 3/);
  assert.match(bad, /<pre class="cmd-err">boom<\/pre>/);
  const refused = renderCommandsHtml({ commandExecutions: [run({ exitCode: null, ok: false, stdout: '', stderr: 'this exact command is not on the allowlist for that repo', durationMs: null })] });
  assert.match(refused, /<summary>Did not run: /);
  assert.match(refused, /not on the allowlist/);
});

test('every field is escaped', () => {
  const evil = '<img src=x onerror=alert(1)>"\'&';
  const html = renderCommandsHtml({
    commandProposals: [proposal({ proposalId: evil, argv: [evil], repo: evil, why: evil })],
    commandExecutions: [run({ argv: [evil], stdout: evil, stderr: evil, confirmedAt: evil, exitCode: evil })],
  });
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;&quot;&#39;&amp;/);
});

test('chat.js imports the renderer, shows it after the tools block, and wires the buttons to the confirm/deny route', () => {
  const src = read('../static/chat.js');
  assert.match(src, /import \{ renderCommandsHtml \} from "\.\/commands-view\.js";/);
  assert.match(src, /\$\{renderToolsHtml\(t\.tools\)\}\$\{renderCommandsHtml\(t\)\}\$\{cards\}/);
  assert.match(src, /e\.target\.closest\('\.cmd-confirm, \.cmd-deny'\)/);
  assert.match(src, /api\/chat\/\$\{encodeURIComponent\(this\.session\.id\)\}\/command\/\$\{encodeURIComponent\(card\.dataset\.proposal\)\}\/\$\{confirm \? 'confirm' : 'deny'\}/);
  assert.match(src, /method: 'POST' \}\);\s*\n\s*const body = await resp\.json/);
});

test('the cards are styled with the palette tokens and Confirm / Deny have 44px touch targets on coarse pointers', () => {
  const css = read('../static/chat.css').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const sel of ['.cmd-card', '.cmd-pending', '.cmd-argv', '.cmd-actions', '.cmd-run', '.cmd-run-bad', '.cmd-out', '.cmd-err']) assert.ok(css.includes(sel), sel + ' missing');
  const coarse = css.slice(css.indexOf('@media (pointer: coarse)'));
  const block = coarse.slice(0, coarse.indexOf('}') + 1);
  assert.match(block, /\.cmd-confirm,/);
  assert.match(block, /\.cmd-deny,/);
  assert.match(block, /min-height: 44px/);
});
