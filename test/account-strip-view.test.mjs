import test from 'node:test';
import assert from 'node:assert/strict';
import { barLevel, windowLabel, formatAccount, bannerStep } from '../static/account-strip-view.js';

const T0 = 1000000;
const FRESH = { dismissed: false, shownAt: null, visible: false };
const step = (state, percent, now, extra = {}) => bannerStep(state, { percent, now, dismissAction: false, live: true, ...extra });

test('barLevel boundaries', () => {
  assert.equal(barLevel(69.9), 'ok');
  assert.equal(barLevel(70), 'amber');
  assert.equal(barLevel(84.9), 'amber');
  assert.equal(barLevel(85), 'red');
  assert.equal(barLevel(null), 'unknown');
  assert.equal(barLevel(NaN), 'unknown');
  assert.equal(barLevel(undefined), 'unknown');
  assert.equal(barLevel('90'), 'unknown');
});

test('windowLabel', () => {
  assert.equal(windowLabel({ percent: 41.6, reset: false }), '42%');
  assert.equal(windowLabel({ percent: null, reset: true }), 'reset');
  assert.equal(windowLabel({ percent: 50, reset: true }), 'reset');
  assert.equal(windowLabel({ percent: null, reset: false }), '--');
  assert.equal(windowLabel(null), '--');
});

test('formatAccount with usage, a reset window and stale', () => {
  const out = formatAccount({
    label: 'work', host: 'wok', live: true,
    usage: { fiveHour: { percent: 72, resetsAt: 5, reset: false }, weekly: { percent: null, resetsAt: 4, reset: true }, stale: true },
  });
  assert.deepEqual(out, {
    label: 'work', host: 'wok', live: true,
    fiveHour: { text: '72%', level: 'amber' },
    weekly: { text: 'reset', level: 'unknown' },
    stale: true,
  });
});

test('formatAccount without a usage snapshot', () => {
  assert.deepEqual(formatAccount({ label: 'a', host: 'h', live: false, usage: null }), {
    label: 'a', host: 'h', live: false,
    fiveHour: { text: '--', level: 'unknown' }, weekly: { text: '--', level: 'unknown' }, stale: false,
  });
});

test('banner shows at 90, not at 89.9', () => {
  assert.equal(step(FRESH, 89.9, T0).show, false);
  assert.equal(step(FRESH, 90, T0).show, true);
});

test('banner stays shown while the percent drops, until dismissed', () => {
  let r = step(FRESH, 95, T0);
  r = step(r.state, 60, T0 + 1000);
  assert.equal(r.show, true);
  r = bannerStep(r.state, { percent: 60, now: T0 + 2000, dismissAction: true, live: true });
  assert.equal(r.show, false);
});

test('after a dismiss 88 stays hidden and 79 re-arms', () => {
  let r = step(FRESH, 95, T0);
  r = bannerStep(r.state, { percent: 95, now: T0 + 1, dismissAction: true, live: true });
  r = step(r.state, 88, T0 + 400000);
  assert.equal(r.show, false);
  r = step(r.state, 91, T0 + 400001);
  assert.equal(r.show, false, 'still dismissed: never fell below 80');
  r = step(r.state, 79, T0 + 400002);
  assert.equal(r.state.dismissed, false);
  r = step(r.state, 92, T0 + 400003);
  assert.equal(r.show, true, 'cooldown is over and it re-armed');
});

test('cooldown blocks a re-show within 5 minutes and allows it after', () => {
  let r = step(FRESH, 95, T0);
  r = bannerStep(r.state, { percent: 95, now: T0 + 1, dismissAction: true, live: true });
  r = step(r.state, 70, T0 + 2);
  assert.equal(r.state.dismissed, false);
  r = step(r.state, 95, T0 + 299999);
  assert.equal(r.show, false);
  r = step(r.state, 95, T0 + 300000);
  assert.equal(r.show, true);
  assert.equal(r.state.shownAt, T0 + 300000);
});

test('a non-live account and a null percent never show it', () => {
  assert.equal(step(FRESH, 99, T0, { live: false }).show, false);
  assert.equal(step(FRESH, null, T0).show, false);
  assert.equal(step(FRESH, NaN, T0).show, false);
  const shown = step(FRESH, 99, T0);
  assert.equal(step(shown.state, null, T0 + 1).show, false);
  assert.equal(step(shown.state, 99, T0 + 1, { live: false }).show, false);
});

test('an unreadable percent hides a shown banner and it does not come back by itself', () => {
  let r = step(FRESH, 99, T0);
  r = step(r.state, null, T0 + 1);
  assert.equal(r.state.visible, false);
  r = step(r.state, 60, T0 + 2);
  assert.equal(r.show, false);
});

test('missing state and input are tolerated', () => {
  assert.equal(bannerStep(undefined, undefined).show, false);
  assert.equal(bannerStep({}, { percent: 91, now: T0, live: true }).show, true);
});
