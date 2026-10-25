// DOM-free helpers for the mid-conversation model-swap UI (chat.js renders; this module
// only decides WHAT to show). Same style as static/sessions-view.js: pure functions, no
// document/window, no network, testable with `node --test` (test/swap-view.test.mjs).
//
// HUB0008 piece 9/11. This module is consumed by piece 10/11 (chat.js); the route that
// builds the swapOffer object is piece 8/11 and the swap POST endpoint is piece 7/11 --
// neither is imported here, only their data shapes are assumed (noted per function).

// 'Claude · <model> · <account>' (account part reads 'live account' when the session has
// no pinned account), 'Local · <model>' for the local provider, 'Unknown' otherwise.
// session.account key assumed from task text: chat_sessions.py defines it as the
// accounts.json LABEL (None = the live account at turn time).
export function providerLabel(session) {
  const s = session || {};
  if (s.provider === 'claude') {
    // 'live account' only when account is null/undefined.
    const account = s.account != null ? s.account : 'live account';
    // A session with no pinned model (null/undefined/'') leaves the model part out instead of printing 'null'.
    return s.model ? 'Claude · ' + s.model + ' · ' + account : 'Claude · ' + account;
  }
  if (s.provider === 'local') return s.model ? 'Local · ' + s.model : 'Local';
  return 'Unknown';
}

// The Swap menu: one entry per registered account (GET /api/chat/accounts shape:
// {accounts: [{label, host, live}]}) except the one the session already uses, plus the
// local model -- unless the session is already on local.
// The account options carry the account's LABEL as `account` so swapBody can echo it
// back: chat_sessions.py (HUB0008 4/11) stores session.account as that label, and the
// accounts endpoints address accounts by label. The local entry never carries an account.
export function swapOptions(accounts, session) {
  const s = session || {};
  const list = (Array.isArray(accounts) ? accounts : []).map((a) => ({
    label: a.label,
    provider: 'claude',
    account: a.label,
  }));
  // Exclude the session's pinned account (by label, per the schema comment in
  // chat_sessions.py). A null/undefined account means "live account" and cannot be
  // matched to one concrete entry, so nothing is dropped in that case.
  const current = s.account;
  const claude = current == null ? list : list.filter((o) => o.account !== current);
  if (s.provider !== 'local') claude.push({ label: 'Local model', provider: 'local' });
  return claude;
}

// The one-line offer the failed-turn flow should show, from the route's swapOffer
// {kind, options}. kind values 'usage'/'rate' and the single-target (options[0])
// assumption come from the task's two template strings; the producing route (HUB0008
// 8/11) is not in this checkout -- confirm the kind strings against it. An unknown kind,
// a missing offer, or an offer with no options yields '' (nothing to offer).
export function offerText(offer) {
  if (!offer || !Array.isArray(offer.options) || offer.options.length === 0) return '';
  const label = offer.options[0].label;
  if (offer.kind === 'usage') return 'Usage limit reached. Switch to ' + label + '?';
  if (offer.kind === 'rate') return 'Rate limited. Switch to ' + label + '?';
  return '';
}

// The POST /api/chat/<id>/swap body for one option: {provider, model?, account?},
// omitting any key whose value would be undefined so the serialized body never carries
// explicit undefined keys. A local option sends provider alone.
export function swapBody(option) {
  const o = option || {};
  const body = { provider: o.provider };
  if (o.model != null) body.model = o.model;
  if (o.account != null) body.account = o.account;
  return body;
}
