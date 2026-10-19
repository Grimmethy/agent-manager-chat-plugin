// DOM-free helpers for the session switcher (chat.js renders; this module only decides
// WHAT to show). Kept free of document/window/Date.now() so it can be unit-tested with
// `node --test` (test/sessions-view.test.mjs): the current time is always injected.

const TITLE_MAX = 40;

export function truncateTitle(title, max = TITLE_MAX) {
  const text = String(title == null ? '' : title).replace(/\s+/g, ' ').trim();
  if (!text) return 'New chat';
  return text.length <= max ? text : text.slice(0, max - 1) + '…';
}

export function statusWord(row) {
  return row && row.busy ? 'Busy' : 'Idle';
}

// 'now' and the ISO string may both be Date or ms; returns whole seconds, clamped at 0, or
// null when the timestamp is missing/unparseable.
export function secondsSince(iso, now) {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const nowMs = now instanceof Date ? now.getTime() : Number(now);
  if (Number.isNaN(nowMs)) return null;
  return Math.max(0, Math.floor((nowMs - then) / 1000));
}

export function formatDuration(seconds) {
  if (seconds < 60) return seconds + 's';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + 'm';
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + 'h';
  return Math.floor(hours / 24) + 'd';
}

// Idle chat: 'idle 5m' from lastActivityAt (or startedAt for a chat nothing was said in).
// Busy chat: just 'busy'.
export function timerLabel(row, now) {
  if (row && row.busy) return 'busy';
  const secs = secondsSince((row && (row.lastActivityAt || row.startedAt)) || null, now);
  return secs == null ? 'idle' : 'idle ' + formatDuration(secs);
}

export function providerLabel(row) {
  const provider = row && row.provider === 'claude' ? 'Claude' : 'Local';
  return row && row.model ? provider + ' · ' + row.model : provider;
}

// Most recently used first; the server already sorts this way, but the client re-sorts so
// it never depends on that.
export function orderSessions(rows) {
  const key = (r) => (r.lastActivityAt || '') > (r.startedAt || '') ? (r.lastActivityAt || '') : (r.startedAt || '');
  return [...rows].sort((a, b) => (key(a) < key(b) ? 1 : key(a) > key(b) ? -1 : 0));
}

// After closing `closedId`, which chat to select: the most recent remaining one, or null
// (the caller then creates a fresh chat).
export function nextSelectionAfterClose(rows, closedId) {
  const remaining = orderSessions((rows || []).filter((r) => r.id !== closedId));
  return remaining.length ? remaining[0].id : null;
}

// Everything one dropdown row displays. `context` is a deliberate placeholder: plugin chats
// do not report a context-token figure yet. When the backend adds one, fill it in here
// (e.g. '12k ctx') and chat.js will render it with no other change.
export function formatRow(row, now) {
  return {
    id: row.id,
    title: truncateTitle(row.title),
    fullTitle: row.title || '',
    provider: providerLabel(row),
    status: statusWord(row),
    busy: !!row.busy,
    timer: timerLabel(row, now),
    turns: (row.turnCount || 0) + (row.turnCount === 1 ? ' turn' : ' turns'),
    context: null, // TODO(context): not reported by plugin chats yet -- see comment above
  };
}
