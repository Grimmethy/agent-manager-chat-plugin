import test from 'node:test';
import assert from 'node:assert/strict';
import { providerLabel, swapOptions, offerText, swapBody } from '../static/swap-view.js';

// Fake account registry in the GET /api/chat/accounts shape -- 'token-a-1234' is a fake
// string, never a real credential.
const ACCOUNTS = [
  { label: 'work', host: 'claude.internal:8080', live: true },
  { label: 'personal', host: 'claude.internal:8081', live: false },
];

test('providerLabel: claude with a pinned account', () => {
  assert.equal(
    providerLabel({ provider: 'claude', model: 'opus', account: 'work' }),
    'Claude · opus · work');
});

test("providerLabel: claude null account reads 'live account'", () => {
  assert.equal(
    providerLabel({ provider: 'claude', model: 'sonnet', account: null }),
    'Claude · sonnet · live account');
  assert.equal(
    providerLabel({ provider: 'claude', model: 'opus' }), // no account key at all
    'Claude · opus · live account');
});

test('providerLabel: local and unknown providers', () => {
  assert.equal(providerLabel({ provider: 'local', model: 'qwen2.5' }), 'Local · qwen2.5');
  assert.equal(providerLabel({ provider: 'openai', model: 'gpt' }), 'Unknown');
  assert.equal(providerLabel({}), 'Unknown');
  assert.equal(providerLabel(null), 'Unknown');
});

test('swapOptions: excludes the current account, keeps local', () => {
  const session = { provider: 'claude', model: 'opus', account: 'work' };
  assert.deepEqual(swapOptions(ACCOUNTS, session), [
    { label: 'personal', provider: 'claude', account: 'personal' },
    { label: 'Local model', provider: 'local' },
  ]);
});

test('swapOptions: live account (null) drops nothing, still appends local', () => {
  const session = { provider: 'claude', model: 'opus', account: null };
  assert.deepEqual(swapOptions(ACCOUNTS, session), [
    { label: 'work', provider: 'claude', account: 'work' },
    { label: 'personal', provider: 'claude', account: 'personal' },
    { label: 'Local model', provider: 'local' },
  ]);
});

test('swapOptions: local session omits the local entry and keeps accounts', () => {
  const session = { provider: 'local', model: 'qwen2.5' };
  assert.deepEqual(swapOptions(ACCOUNTS, session), [
    { label: 'work', provider: 'claude', account: 'work' },
    { label: 'personal', provider: 'claude', account: 'personal' },
  ]);
});

test('swapOptions: empty account list still offers local to a claude session', () => {
  assert.deepEqual(swapOptions([], { provider: 'claude', model: 'opus', account: null }),
    [{ label: 'Local model', provider: 'local' }]);
});

test('offerText: usage and rate kinds, empty/missing offer, unknown kind', () => {
  assert.equal(
    offerText({ kind: 'usage', options: [{ label: 'personal' }] }),
    'Usage limit reached. Switch to personal?');
  assert.equal(
    offerText({ kind: 'rate', options: [{ label: 'Local model' }] }),
    'Rate limited. Switch to Local model?');
  assert.equal(offerText(null), '');
  assert.equal(offerText(undefined), '');
  assert.equal(offerText({}), '');
  assert.equal(offerText({ kind: 'usage', options: [] }), '');
  assert.equal(offerText({ kind: 'other', options: [{ label: 'work' }] }), '');
});

test('swapBody: shapes with no undefined keys', () => {
  // claude with account, with and without model
  assert.deepEqual(
    swapBody({ label: 'work', provider: 'claude', account: 'work' }),
    { provider: 'claude', account: 'work' });
  assert.deepEqual(
    swapBody({ provider: 'claude', model: 'opus', account: 'personal' }),
    { provider: 'claude', model: 'opus', account: 'personal' });
  // local: provider only, never account/model
  const local = swapBody({ label: 'Local model', provider: 'local' });
  assert.deepEqual(local, { provider: 'local' });
  assert.ok(!Object.hasOwn(local, 'model'));
  assert.ok(!Object.hasOwn(local, 'account'));
  // no undefined keys leak in
  const claude = swapBody({ provider: 'claude', model: undefined, account: undefined });
  assert.deepEqual(Object.keys(claude), ['provider']);
});
