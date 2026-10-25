// DOM-free renderer for the tools an assistant turn ran (2026-10-25). chat.js puts the returned HTML under the assistant message; tool_log.py makes
// the stored entries: [{ name, args, ok, summary }]. Everything is escaped here, nothing is trusted. Testable with `node --test` (test/tools-view.test.mjs).

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// '' when there is nothing to show, else a collapsed <details> ('Ran N tools') with one row per tool: name, arguments, one-line outcome.
export function renderToolsHtml(tools) {
  const list = Array.isArray(tools) ? tools.filter((t) => t && typeof t === 'object') : [];
  if (!list.length) return '';
  const failed = list.filter((t) => t.ok === false).length;
  const head = `Ran ${list.length} tool${list.length === 1 ? '' : 's'}` + (failed ? ` (${failed} failed)` : '');
  const rows = list.map((t) => (
    `<div class="chat-tool-row${t.ok === false ? ' chat-tool-bad' : ''}">` +
    `<span class="chat-tool-name">${esc(t.name)}</span>` +
    `<span class="chat-tool-args">${esc(t.args)}</span>` +
    `<span class="chat-tool-outcome">${esc(t.summary)}</span></div>`
  )).join('');
  return `<details class="chat-tools"><summary>${esc(head)}</summary>${rows}</details>`;
}
