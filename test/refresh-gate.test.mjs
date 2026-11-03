// static/refresh-gate.js: the rate gate behind SessionSwitcher.refresh() (2026-11-02, GET /api/chat/terminal-sessions
// was seen at 22 requests per second). A fake clock and fake timers drive it, so nothing here waits in real time.
// Run: node --test test/refresh-gate.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRefreshGate } from '../static/refresh-gate.js';

// A controllable clock plus a timer list; fire() runs every timer whose time has come.
function fakeEnv() {
  let t = 1000;
  const timers = [];
  return {
    now: () => t,
    setTimer: (cb, ms) => { timers.push({ at: t + ms, cb }); return timers.length; },
    advance(ms) {
      t += ms;
      for (const x of timers.filter((y) => y.at <= t && !y.done)) { x.done = true; x.cb(); }
    },
    pendingTimers: () => timers.filter((x) => !x.done).length,
  };
}

// A fn whose runs finish only when the test says so.
function manualFn() {
  const runs = [];
  const fn = () => new Promise((resolve, reject) => { runs.push({ resolve, reject }); });
  return { fn, runs };
}

test('50 synchronous calls while a run is in flight cause exactly one fn call and share one promise', async () => {
  const env = fakeEnv();
  const { fn, runs } = manualFn();
  const gated = createRefreshGate(fn, { minGapMs: 1000, now: env.now, setTimer: env.setTimer });
  const promises = [];
  for (let i = 0; i < 50; i++) promises.push(gated());
  await Promise.resolve();
  assert.equal(runs.length, 1);
  assert.ok(promises.every((p) => p === promises[0]), 'every caller gets the same promise');
  runs[0].resolve('rows');
  assert.equal(await promises[0], 'rows');
});

test('calls inside the gap share exactly one trailing run, started when the gap ends', async () => {
  const env = fakeEnv();
  let n = 0;
  const gated = createRefreshGate(async () => ++n, { minGapMs: 1000, now: env.now, setTimer: env.setTimer });
  assert.equal(await gated(), 1);
  env.advance(100);
  const inGap = [];
  for (let i = 0; i < 20; i++) inGap.push(gated());
  assert.equal(env.pendingTimers(), 1, 'one trailing timer however many calls arrive');
  assert.equal(n, 1, 'nothing ran yet');
  env.advance(900);
  const results = await Promise.all(inGap);
  assert.equal(n, 2, 'exactly one trailing run');
  assert.ok(results.every((r) => r === 2));
});

test('a call after the gap has passed runs at once with no timer', async () => {
  const env = fakeEnv();
  let n = 0;
  const gated = createRefreshGate(async () => ++n, { minGapMs: 1000, now: env.now, setTimer: env.setTimer });
  await gated();
  env.advance(1500);
  assert.equal(await gated(), 2);
  assert.equal(env.pendingTimers(), 0);
});

test('a rejecting fn rejects that call only and the next call still runs', async () => {
  const env = fakeEnv();
  let n = 0;
  const gated = createRefreshGate(async () => { n++; if (n === 1) throw new Error('boom'); return n; },
    { minGapMs: 1000, now: env.now, setTimer: env.setTimer });
  await assert.rejects(() => gated(), /boom/);
  env.advance(1500);
  assert.equal(await gated(), 2);
});

test('the promise returned by a gated call resolves to the value fn returned', async () => {
  const env = fakeEnv();
  const gated = createRefreshGate(async () => ['row-a', 'row-b'], { now: env.now, setTimer: env.setTimer });
  assert.deepEqual(await gated(), ['row-a', 'row-b']);
});

test('chat.js wires SessionSwitcher.refresh through the gate and keeps the old body as _refreshNow', () => {
  const chatJs = readFileSync(fileURLToPath(new URL('../static/chat.js', import.meta.url)), 'utf8');
  assert.match(chatJs, /import \{ createRefreshGate \} from "\.\/refresh-gate\.js";/);
  assert.match(chatJs, /async _refreshNow\(\) \{/);
  assert.match(chatJs, /this\.refresh = createRefreshGate\(\(\) => this\._refreshNow\(\)/);
  assert.doesNotMatch(chatJs, /async refresh\(\) \{/, 'the ungated method must be gone');
});
