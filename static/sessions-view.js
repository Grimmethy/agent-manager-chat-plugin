// DOM-free helpers for the session switcher (chat.js renders; this module only decides
// WHAT to show). Kept free of document/window/Date.now() so it can be unit-tested with
// `node --test` (test/sessions-view.test.mjs): the current time is always injected.

const TITLE_MAX = 40;
const ROW_TITLE_MAX = 200;

export function truncateTitle(title, max = TITLE_MAX) {
  const text = String(title == null ? '' : title).replace(/\s+/g, ' ').trim();
  if (!text) return 'New chat';
  return text.length <= max ? text : text.slice(0, max - 1) + '…';
}

// Row/button title: the whitespace-normalised FULL title, cut only beyond
// ROW_TITLE_MAX (the row and the button clip themselves visually, so a short
// JS cut just hid text the reader could see). 'New chat' when there is none.
export function fullTitle(raw) {
  const text = String(raw == null ? '' : raw).replace(/\s+/g, ' ').trim();
  if (!text) return 'New chat';
  return text.length <= ROW_TITLE_MAX ? text : text.slice(0, ROW_TITLE_MAX) + '…';
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
    title: fullTitle(row.title),
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

export function rowKey(row) {
  return (row.host || '') + '/' + row.id;
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
    host: row.host || '',
    key: rowKey(row),
  };
}

export function hostsOf(rows) {
  const set = new Set();
  for (const r of rows) {
    const h = r.host || '';
    if (h !== '') set.add(h);
  }
  return [...set].sort();
}

export function filterByHost(rows, host) {
  if (host === '' || host == null) return rows;
  return rows.filter((r) => (r.host || '') === host);
}

// Type-to-filter (HUB0010 8/10): case-insensitive substring match of the trimmed query
// against a row's name, cwd and host (a missing field counts as empty). An empty or
// whitespace-only query returns `rows` itself, in the order given.
export function filterRows(rows, query) {
  const needle = String(query == null ? '' : query).trim().toLowerCase();
  if (needle === '') return rows;
  const has = (value) => String(value == null ? '' : value).toLowerCase().includes(needle);
  return rows.filter((r) => !!r && (has(r.name) || has(r.cwd) || has(r.host)));
}

// Same filter for the CHAT rows (the switcher box is labelled 'Filter chats'): case-insensitive substring of
// the trimmed query against the full title and the provider/model label ('Local · qwen3'). An empty or
// whitespace-only query returns `rows` itself; a null row or a missing field never throws.
export function filterChats(rows, query) {
  const needle = String(query == null ? '' : query).trim().toLowerCase();
  if (needle === '') return rows;
  const has = (value) => String(value == null ? '' : value).toLowerCase().includes(needle);
  return rows.filter((r) => !!r && (has(r.title) || has(providerLabel(r)) || has(r.model)));
}

// Ctrl+1..9 jump: the key of the nth row of the order GIVEN (the caller passes the order on
// screen), or null for n outside 1-9 or past the end. The key is host/id when the row has a
// host and just the id otherwise: the same form the attention code uses for newly waiting
// sessions (not rowKey, which always carries the slash).
export function jumpTarget(rows, n) {
  if (!Array.isArray(rows) || !Number.isInteger(n) || n < 1 || n > 9) return null;
  const row = rows[n - 1];
  if (!row) return null;
  return row.host ? row.host + '/' + row.id : row.id;
}

// Waiting (needs you) first, then busy, then idle; ties by host, then title, so the list
// does not jump.
export function orderTerminalSessions(rows) {
  const rank = { waiting: 0, busy: 1, idle: 2 };
  const key = (r) => (Object.hasOwn(rank, r.status) ? rank[r.status] : 2);
  return [...rows].sort((a, b) => {
    const byStatus = key(a) - key(b);
    if (byStatus !== 0) return byStatus;
    const hostA = a.host || '';
    const hostB = b.host || '';
    if (hostA !== hostB) return hostA.localeCompare(hostB);
    return String(a.name || '').localeCompare(String(b.name || ''));
  });
}

// SSE reducer: 'session.updated' upserts a full row, 'session.removed' drops it; anything
// else (e.g. 'transcript.append') leaves the rows alone. Never mutates its input.
// When data carries a non-empty host, match host-aware (rowKey) so a session id shared
// across hosts stays distinct; otherwise fall back to id-only matching.
function matchesRow(row, data) {
  if (typeof data.host === 'string' && data.host !== '') return rowKey(row) === rowKey(data);
  return row.id === data.id;
}

export function applyTerminalEvent(rows, event, data) {
  if (!data || typeof data.id !== 'string') return rows;
  if (event === 'session.removed') return rows.filter((r) => !matchesRow(r, data));
  if (event !== 'session.updated') return rows;
  const i = rows.findIndex((r) => matchesRow(r, data));
  if (i === -1) return [...rows, data];
  const next = [...rows];
  next[i] = data;
  return next;
}

// The empty-state line for the terminal group: names the host(s) that were asked (the `hosts` array GET api/chat/terminal-sessions
// already returns) so an operator can tell the feature is pointed at the wrong machine. Plain text, the caller escapes it. Non-string and
// blank entries are ignored; no usable host gives the plain 'No terminal sessions'. No URL or token ever reaches this.
export function emptyLabel(hosts) {
  const names = Array.isArray(hosts) ? hosts.filter((h) => typeof h === 'string' && h.trim() !== '') : [];
  return names.length ? 'No terminal sessions on ' + names.join(', ') : 'No terminal sessions';
}

// The line naming the hosts whose session agent could not be reached. GET api/chat/terminal-sessions reports each as {host, reason} (agent_client.list_all);
// an older shape, a bare host name, still works. Returns plain text ('grimm (ConnectionError), wok'), '' when nothing usable: the caller escapes it.
export function skippedLabel(skipped) {
  if (!Array.isArray(skipped)) return '';
  const parts = [];
  for (const s of skipped) {
    if (typeof s === 'string' && s !== '') parts.push(s);
    else if (s && typeof s === 'object' && typeof s.host === 'string' && s.host !== '') {
      parts.push(typeof s.reason === 'string' && s.reason !== '' ? `${s.host} (${s.reason})` : s.host);
    }
  }
  return parts.join(', ');
}

