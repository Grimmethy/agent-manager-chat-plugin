// DOM-free renderer for the owner-approved write commands on an assistant turn (2026-10-25; design: docs/write-command-allowlist-design.md section 6).
// command_runs.py stores `commandProposals: [{ proposalId, repo, argv, why, status, decidedAt }]` and `commandExecutions: [{ proposalId, argv, repo, confirmedAt,
// exitCode, ok, stdout, stderr, durationMs }]` on the assistant entry; chat.js puts the HTML returned here under the message. A pending proposal gets the Confirm and
// Deny buttons (handled by chat.js through the .cmd-confirm / .cmd-deny classes and the card's data-proposal id); everything else is read-only text. Every field is
// escaped here, nothing is trusted. Testable with `node --test` (test/commands-view.test.mjs).

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const STATUS_TEXT = {
  pending: 'Waiting for your decision -- nothing has run.',
  confirmed: 'Confirmed -- running (or core has not reported back yet).',
  denied: 'Denied -- it was not run.',
  ran: 'Ran.',
};

function argvText(argv) {
  return Array.isArray(argv) ? argv.map((a) => String(a)).join(' ') : '';
}

function proposalHtml(p) {
  const status = Object.prototype.hasOwnProperty.call(STATUS_TEXT, p.status) ? p.status : 'pending';
  const actions = status === 'pending'
    ? `<div class="cmd-actions"><button type="button" class="action cmd-confirm">Confirm and run</button><button type="button" class="secondary cmd-deny">Deny</button></div>`
    : '';
  return `<div class="cmd-card cmd-${status}" data-proposal="${esc(p.proposalId)}" data-status="${status}">`
    + '<div class="cmd-head">Command proposed</div>'
    + `<code class="cmd-argv">${esc(argvText(p.argv))}</code>`
    + `<div class="cmd-meta">in ${esc(p.repo)}</div>`
    + (p.why ? `<div class="cmd-why">${esc(p.why)}</div>` : '')
    + `<div class="cmd-status">${esc(STATUS_TEXT[status])}</div>`
    + actions
    + '</div>';
}

function executionHtml(r) {
  const didNotRun = r.exitCode === null || r.exitCode === undefined;
  const head = didNotRun ? 'Did not run' : `Exit code ${esc(r.exitCode)}${typeof r.durationMs === 'number' ? ` in ${esc((r.durationMs / 1000).toFixed(1))} s` : ''}`;
  const parts = [];
  if (r.stdout) parts.push(`<pre class="cmd-out">${esc(r.stdout)}</pre>`);
  if (r.stderr) parts.push(`<pre class="cmd-err">${esc(r.stderr)}</pre>`);
  return `<details class="cmd-run${r.ok ? '' : ' cmd-run-bad'}" open>`
    + `<summary>${esc(head)}: ${esc(argvText(r.argv))}</summary>`
    + (r.confirmedAt ? `<div class="cmd-meta">confirmed ${esc(r.confirmedAt)}</div>` : '')
    + parts.join('')
    + '</details>';
}

// '' when the entry has no proposals and no executions.
export function renderCommandsHtml(entry) {
  const e = entry && typeof entry === 'object' ? entry : {};
  const proposals = Array.isArray(e.commandProposals) ? e.commandProposals.filter((p) => p && typeof p === 'object' && p.proposalId) : [];
  const runs = Array.isArray(e.commandExecutions) ? e.commandExecutions.filter((r) => r && typeof r === 'object') : [];
  if (!proposals.length && !runs.length) return '';
  return `<div class="cmd-block">${proposals.map(proposalHtml).join('')}${runs.map(executionHtml).join('')}</div>`;
}
