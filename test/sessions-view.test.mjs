import test from 'node:test';
import assert from 'node:assert/strict';
import {
  truncateTitle, statusWord, secondsSince, formatDuration, timerLabel, providerLabel,
  orderSessions, nextSelectionAfterClose, formatRow,
  formatTokens, contextLabel, terminalTimer, STALE_BUSY_SECS, formatTerminalRow, orderTerminalSessions, applyTerminalEvent,
} from '../static/sessions-view.js';
import { rowKey, hostsOf, filterByHost } from '../static/sessions-view.js';

const NOW = Date.parse('2026-10-19T12:00:00Z');

test('truncateTitle', () => {
  assert.equal(truncateTitle('short'), 'short');
  assert.equal(truncateTitle(''), 'New chat');
  assert.equal(truncateTitle(null), 'New chat');
  assert.equal(truncateTitle('a  b\n c'), 'a b c');
  const long = 'x'.repeat(100);
  const out = truncateTitle(long, 10);
  assert.equal(out.length, 10);
  assert.ok(out.endsWith('…'));
});

test('statusWord', () => {
  assert.equal(statusWord({ busy: true }), 'Busy');
  assert.equal(statusWord({ busy: false }), 'Idle');
  assert.equal(statusWord({}), 'Idle');
});

test('secondsSince', () => {
  assert.equal(secondsSince('2026-10-19T11:55:00Z', NOW), 300);
  assert.equal(secondsSince('2026-10-19T12:00:05Z', NOW), 0); // future clamps
  assert.equal(secondsSince(null, NOW), null);
  assert.equal(secondsSince('garbage', NOW), null);
  assert.equal(secondsSince('2026-10-19T11:59:00Z', new Date(NOW)), 60);
});

test('formatDuration', () => {
  assert.equal(formatDuration(5), '5s');
  assert.equal(formatDuration(300), '5m');
  assert.equal(formatDuration(3600 * 3 + 5), '3h');
  assert.equal(formatDuration(86400 * 2), '2d');
});

test('timerLabel: idle uses lastActivityAt, falls back to startedAt, busy is just busy', () => {
  assert.equal(timerLabel({ lastActivityAt: '2026-10-19T11:55:00Z', startedAt: '2026-10-19T10:00:00Z' }, NOW), 'idle 5m');
  assert.equal(timerLabel({ lastActivityAt: null, startedAt: '2026-10-19T09:00:00Z' }, NOW), 'idle 3h');
  assert.equal(timerLabel({ busy: true, lastActivityAt: '2026-10-19T11:55:00Z' }, NOW), 'busy');
  assert.equal(timerLabel({}, NOW), 'idle');
});

test('providerLabel', () => {
  assert.equal(providerLabel({ provider: 'local', model: null }), 'Local');
  assert.equal(providerLabel({ provider: 'claude', model: 'opus' }), 'Claude · opus');
});

test('orderSessions: most recent first, does not mutate', () => {
  const rows = [
    { id: 'a', startedAt: '2026-10-19T09:00:00Z', lastActivityAt: '2026-10-19T09:05:00Z' },
    { id: 'b', startedAt: '2026-10-19T10:00:00Z', lastActivityAt: null },
    { id: 'c', startedAt: '2026-10-19T08:00:00Z', lastActivityAt: '2026-10-19T11:00:00Z' },
  ];
  assert.deepEqual(orderSessions(rows).map((r) => r.id), ['c', 'b', 'a']);
  assert.deepEqual(rows.map((r) => r.id), ['a', 'b', 'c']);
});

test('nextSelectionAfterClose', () => {
  const rows = [
    { id: 'a', startedAt: '2026-10-19T09:00:00Z' },
    { id: 'b', startedAt: '2026-10-19T10:00:00Z' },
  ];
  assert.equal(nextSelectionAfterClose(rows, 'b'), 'a');
  assert.equal(nextSelectionAfterClose([rows[0]], 'a'), null);
  assert.equal(nextSelectionAfterClose([], 'a'), null);
});

test('formatRow', () => {
  const r = formatRow({
    id: 'x', title: 'hello', provider: 'claude', model: 'opus', busy: false,
    startedAt: '2026-10-19T11:00:00Z', lastActivityAt: '2026-10-19T11:55:00Z', turnCount: 1,
  }, NOW);
  assert.equal(r.title, 'hello');
  assert.equal(r.provider, 'Claude · opus');
  assert.equal(r.status, 'Idle');
  assert.equal(r.timer, 'idle 5m');
  assert.equal(r.turns, '1 turn');
  assert.equal(r.context, null);
  assert.equal(formatRow({ id: 'y', turnCount: 0, busy: true }, NOW).turns, '0 turns');
});

test('formatTokens / contextLabel: window null shows tokens alone', () => {
  assert.equal(formatTokens(999), '999');
  assert.equal(formatTokens(138028), '138k');
  assert.equal(formatTokens(1500000), '1.5M');
  assert.equal(formatTokens(null), null);
  assert.equal(contextLabel(138028, null), '138k tokens');
  assert.equal(contextLabel(50000, 200000), '50k / 200k (25%)');
  assert.equal(contextLabel(null, 200000), null);
  assert.equal(contextLabel(5, 0), '5 tokens');
});

test('terminalTimer / formatTerminalRow', () => {
  const row = { id: 'a', name: 'proj-1', cwd: '/x/y/proj', status: 'busy', statusSince: '2026-10-19T11:57:00Z',
    step: 'Bash: ls', contextTokens: 2000, contextWindow: null, model: 'm' };
  assert.equal(terminalTimer(row, NOW), 'busy 3m');
  assert.equal(terminalTimer({ status: 'waiting' }, NOW), 'waiting');
  const fmt = formatTerminalRow(row, NOW);
  assert.deepEqual([fmt.title, fmt.status, fmt.busy, fmt.waiting, fmt.step, fmt.context],
    ['proj-1', 'Busy', true, false, 'Bash: ls', '2k tokens']);
  assert.equal(formatTerminalRow({ id: 'b', cwd: '/x/y/proj', status: 'weird' }, NOW).title, 'proj');
  assert.equal(formatTerminalRow({ id: 'b', status: 'weird' }, NOW).status, 'Idle');
});

test('orderTerminalSessions: waiting, busy, idle', () => {
  const rows = [{ id: '1', name: 'b', status: 'idle' }, { id: '2', name: 'a', status: 'busy' },
    { id: '3', name: 'c', status: 'waiting' }, { id: '4', name: 'a', status: 'idle' }];
  assert.deepEqual(orderTerminalSessions(rows).map((r) => r.id), ['3', '2', '4', '1']);
});

test('applyTerminalEvent', () => {
  const a = { id: 'a', status: 'idle' };
  let rows = applyTerminalEvent([], 'session.updated', a);
  assert.deepEqual(rows, [a]);
  rows = applyTerminalEvent(rows, 'session.updated', { id: 'a', status: 'busy' });
  assert.deepEqual(rows, [{ id: 'a', status: 'busy' }]);
  assert.equal(applyTerminalEvent(rows, 'transcript.append', { id: 'a', entries: [] }), rows);
  assert.equal(applyTerminalEvent(rows, 'session.updated', {}), rows);
  assert.deepEqual(applyTerminalEvent(rows, 'session.removed', { id: 'a' }), []);
});

test('prototype keys are not statuses', () => {
  assert.equal(formatTerminalRow({ id: 'p', status: 'constructor' }, NOW).status, 'Idle');
  assert.equal(formatTerminalRow({ id: 'p', status: 'toString' }, NOW).busy, false);
  assert.deepEqual(orderTerminalSessions([{ id: '1', name: 'a', status: 'constructor' },
    { id: '2', name: 'b', status: 'busy' }]).map((r) => r.id), ['2', '1']);
});

test('busy with no transcript activity for a long time is flagged, not timed', () => {
  const base = { status: 'busy', statusSince: '2026-10-18T07:00:00Z' };
  assert.equal(terminalTimer({ ...base, lastActivityAt: '2026-10-19T06:00:00Z' }, NOW), 'busy, no activity 6h');
  assert.equal(terminalTimer({ ...base, lastActivityAt: '2026-10-19T11:59:00Z' }, NOW), 'busy 1d');
  assert.equal(terminalTimer({ ...base }, NOW), 'busy 1d'); // unknown activity: plain timer
  assert.equal(terminalTimer({ status: 'idle', statusSince: '2026-10-19T00:00:00Z',
    lastActivityAt: '2026-10-19T00:00:00Z' }, NOW), 'idle 12h'); // only busy rows are flagged
  assert.equal(STALE_BUSY_SECS, 3600);
});

test('host-aware events: the same session id on two hosts coexists', () => {
  let rows = applyTerminalEvent([], 'session.updated', { id: 'a', host: 'h1', status: 'idle' });
  rows = applyTerminalEvent(rows, 'session.updated', { id: 'a', host: 'h2', status: 'busy' });
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map(rowKey).sort(), ['h1/a', 'h2/a']);
});

test('host-aware events: session.removed with a host removes only that host\'s row', () => {
  const rows = [{ id: 'a', host: 'h1' }, { id: 'a', host: 'h2' }, { id: 'b', host: 'h1' }];
  const next = applyTerminalEvent(rows, 'session.removed', { id: 'a', host: 'h1' });
  assert.deepEqual(next, [{ id: 'a', host: 'h2' }, { id: 'b', host: 'h1' }]);
  assert.equal(rows.length, 3, 'the input is not mutated');
});

test('host-aware events: session.updated with a host replaces only that host\'s row', () => {
  const rows = [{ id: 'a', host: 'h1', status: 'idle' }, { id: 'a', host: 'h2', status: 'idle' }];
  const next = applyTerminalEvent(rows, 'session.updated', { id: 'a', host: 'h2', status: 'busy' });
  assert.deepEqual(next, [{ id: 'a', host: 'h1', status: 'idle' }, { id: 'a', host: 'h2', status: 'busy' }]);
});

test('legacy events with no host behave exactly as before (id-only matching)', () => {
  let rows = applyTerminalEvent([], 'session.updated', { id: 'a', status: 'idle' });
  rows = applyTerminalEvent(rows, 'session.updated', { id: 'a', status: 'busy' });
  assert.deepEqual(rows, [{ id: 'a', status: 'busy' }]);
  assert.deepEqual(applyTerminalEvent(rows, 'session.removed', { id: 'a' }), []);
  // a hostless event cannot say which host it means, so it matches the id on every host
  assert.deepEqual(applyTerminalEvent([{ id: 'a', host: 'h1' }, { id: 'a', host: 'h2' }, { id: 'b', host: 'h1' }], 'session.removed', { id: 'a' }), [{ id: 'b', host: 'h1' }]);
});

test('formatTerminalRow carries host and a host-qualified key', () => {
  const withHost = formatTerminalRow({ id: 'a', host: 'h1', status: 'idle' }, NOW);
  assert.equal(withHost.host, 'h1');
  assert.equal(withHost.key, 'h1/a');
  const legacy = formatTerminalRow({ id: 'a', status: 'idle' }, NOW);
  assert.equal(legacy.host, '');
  assert.equal(legacy.key, '/a');
  assert.equal(rowKey({ id: 'a', host: 'h1' }), 'h1/a');
});

test('orderTerminalSessions breaks status ties by host, then name', () => {
  const rows = [
    { id: '1', host: 'hB', name: 'a', status: 'idle' },
    { id: '2', host: 'hA', name: 'z', status: 'idle' },
    { id: '3', host: 'hA', name: 'b', status: 'idle' },
    { id: '4', host: 'hZ', name: 'a', status: 'waiting' },
  ];
  assert.deepEqual(orderTerminalSessions(rows).map((r) => r.id), ['4', '3', '2', '1']);
});

test('hostsOf returns the sorted, unique, non-empty hosts', () => {
  assert.deepEqual(hostsOf([{ host: 'b' }, { host: 'a' }, { host: 'b' }, {}, { host: '' }]), ['a', 'b']);
  assert.deepEqual(hostsOf([]), []);
});

test('filterByHost keeps one host\'s rows, and everything when no host is given', () => {
  const rows = [{ id: '1', host: 'h1' }, { id: '2', host: 'h2' }, { id: '3' }];
  assert.deepEqual(filterByHost(rows, 'h1').map((r) => r.id), ['1']);
  assert.deepEqual(filterByHost(rows, '').map((r) => r.id), ['1', '2', '3']);
  assert.equal(filterByHost(rows, null), rows);
  assert.equal(filterByHost(rows, undefined), rows);
  assert.deepEqual(filterByHost(rows, 'nobody'), []);
});
