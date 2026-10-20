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

// --- Terminal sessions (read-only rows reported by the local session agent) ---------------

export function formatTokens(n) {
  if (!Number.isFinite(n) || n < 0) return null;
  if (n < 1000) return String(Math.round(n));
  if (n < 1000000) return Math.round(n / 1000) + 'k';
  return (n / 1000000).toFixed(1) + 'M';
}

// '138k / 1.0M (14%)' when the agent knows the window, else just '138k tokens' -- the agent
// reports contextWindow:null for models it has no configured size for, and nothing is guessed.
export function contextLabel(tokens, windowSize) {
  const used = formatTokens(tokens);
  if (used == null) return null;
  if (Number.isFinite(windowSize) && windowSize > 0) {
    return used + ' / ' + formatTokens(windowSize) + ' (' + Math.round((tokens / windowSize) * 100) + '%)';
  }
  return used + ' tokens';
}

const STATUS_WORDS = { busy: 'Busy', waiting: 'Waiting', idle: 'Idle' };

export function terminalStatusWord(status) {
  return Object.hasOwn(STATUS_WORDS, status) ? STATUS_WORDS[status] : 'Idle';
}

// 'busy 3m' / 'waiting 12s' / 'idle 2h' from statusSince; the bare word if it is unknown.
// A busy row with no transcript activity for STALE_BUSY_SECS reads 'busy, no activity 29h'.
// A session can show 'busy' for a day with nothing written to its transcript (hung, or its
// process was suspended): past this much silence say so instead of presenting a busy timer.
export const STALE_BUSY_SECS = 3600;

export function terminalTimer(row, now) {
  const word = terminalStatusWord(row && row.status).toLowerCase();
  if (row && row.status === 'busy') {
    const quiet = secondsSince(row.lastActivityAt || null, now);
    if (quiet != null && quiet >= STALE_BUSY_SECS) return 'busy, no activity ' + formatDuration(quiet);
  }
  const secs = secondsSince((row && row.statusSince) || null, now);
  return secs == null ? word : word + ' ' + formatDuration(secs);
}

function lastSegment(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

export function formatTerminalRow(row, now) {
  const status = Object.hasOwn(STATUS_WORDS, row.status) ? row.status : 'idle';
  return {
    id: row.id,
    title: truncateTitle(row.name || lastSegment(row.cwd)),
    fullTitle: row.cwd || '',
    status: terminalStatusWord(status),
    busy: status === 'busy',
    waiting: status === 'waiting',
    timer: terminalTimer(row, now),
    step: row.step || '',
    context: contextLabel(row.contextTokens, row.contextWindow),
    model: row.model || '',
  };
}

// Waiting (needs you) first, then busy, then idle; ties by title so the list does not jump.
export function orderTerminalSessions(rows) {
  const rank = { waiting: 0, busy: 1, idle: 2 };
  const key = (r) => (Object.hasOwn(rank, r.status) ? rank[r.status] : 2);
  return [...rows].sort((a, b) => key(a) - key(b) || String(a.name || '').localeCompare(String(b.name || '')));
}

// SSE reducer: 'session.updated' upserts a full row, 'session.removed' drops it; anything
// else (e.g. 'transcript.append') leaves the rows alone. Never mutates its input.
export function applyTerminalEvent(rows, event, data) {
  if (!data || typeof data.id !== 'string') return rows;
  if (event === 'session.removed') return rows.filter((r) => r.id !== data.id);
  if (event !== 'session.updated') return rows;
  const i = rows.findIndex((r) => r.id === data.id);
  if (i === -1) return [...rows, data];
  const next = [...rows];
  next[i] = data;
  return next;
}
