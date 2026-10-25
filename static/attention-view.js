// static/attention-view.js
// Pure helpers for the "needs you" attention logic (HUB0010 piece 1/10).
// No DOM, no I/O, no external imports.

// Key rule from the task spec: use "host/id" when the row has a host
// (several-agents work), otherwise just the row id. rowKey() does not
// exist in this repo, so the rule is implemented inline here.
function keyOf(row) {
  if (row.host) return (row.host || '') + '/' + row.id;
  return row.id;
}

// prev: {key: status} from the last step, or null on the very first call.
// rows: terminal session rows, each with at least { id, status } and
// optionally { host }.
// Returns { map, newlyWaiting, count }:
//   map           -- new {key: status} for every current row
//   newlyWaiting  -- keys now 'waiting' that were not 'waiting' before
//                    (a key absent from prev counts as newly waiting only
//                    when prev is not null; on the first call, prev is null
//                    and nothing is newly waiting)
//   count         -- how many rows are 'waiting' now
export function attentionStep(prev, rows) {
  const map = {};
  for (const row of rows) {
    map[keyOf(row)] = row.status;
  }

  const count = rows.filter((r) => r.status === 'waiting').length;

  let newlyWaiting = [];
  if (prev !== null) {
    newlyWaiting = Object.keys(map).filter((key) => {
      if (map[key] !== 'waiting') return false;
      // 'was not waiting before': key absent from prev (undefined) or
      // present with a different status.
      return prev[key] !== 'waiting';
    });
  }

  return { map, newlyWaiting, count };
}

// enabled: whether chimes are turned on (a saved setting elsewhere).
// newlyWaiting: keys from attentionStep.
// mountedKeys: array of keys of panes currently on screen.
// Chime only when enabled AND at least one newly waiting key is off screen.
export function shouldChime(newlyWaiting, mountedKeys, enabled) {
  if (!enabled) return false;
  return newlyWaiting.some((key) => !mountedKeys.includes(key));
}
