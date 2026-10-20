import test from 'node:test';
import assert from 'node:assert/strict';
import {
  truncateTitle, statusWord, secondsSince, formatDuration, timerLabel, providerLabel,
  orderSessions, nextSelectionAfterClose, formatRow,
  formatTokens, contextLabel, terminalTimer, formatTerminalRow, orderTerminalSessions, applyTerminalEvent,
} from '../static/sessions-view.js';

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
