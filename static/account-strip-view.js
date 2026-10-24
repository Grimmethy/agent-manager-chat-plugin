// DOM-free helpers for the account strip (chat.js renders; this module only decides WHAT to
// show). Same rules as sessions-view.js: no document/window/Date.now(), the time is injected,
// so it is unit-tested with `node --test test/account-strip-view.test.mjs`.
//
// An account is what GET /api/chat/accounts returns per entry (routes/accounts.py):
//   {label, host, live, usage: {fiveHour: W, weekly: W, stale} | null}
// where W is usage.read_usage's window {percent: number|null, resetsAt, reset: bool}.

const AMBER_FROM = 70;
const RED_FROM = 85;
const BANNER_SHOW_AT = 90;
const BANNER_REARM_BELOW = 80;
const BANNER_COOLDOWN_MS = 300000;

function isPercent(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

export function barLevel(percent) {
  if (!isPercent(percent)) return 'unknown';
  if (percent >= RED_FROM) return 'red';
  if (percent >= AMBER_FROM) return 'amber';
  return 'ok';
}

export function windowLabel(win) {
  if (win && win.reset === true) return 'reset';
  if (win && isPercent(win.percent)) return Math.round(win.percent) + '%';
  return '--';
}

function formatWindow(win) {
  return { text: windowLabel(win), level: barLevel(win && win.reset !== true ? win.percent : null) };
}

export function formatAccount(account) {
  const usage = account && account.usage ? account.usage : null;
  return {
    label: account && account.label != null ? String(account.label) : '',
    host: account && account.host != null ? String(account.host) : '',
    live: !!(account && account.live),
    fiveHour: formatWindow(usage && usage.fiveHour),
    weekly: formatWindow(usage && usage.weekly),
    stale: usage ? usage.stale === true : false,
  };
}

// The "this account is nearly out" banner as pure state. state is
// {dismissed, shownAt, visible}; a state without `visible` counts as not showing.
// input is {percent, now, dismissAction, live}. Returns {state, show}.
//   - only the live account, only with a real percent, shows it, and only at >= 90
//   - once shown it stays shown until dismissed (dismissAction)
//   - after a dismiss it stays hidden until the percent falls below 80, then re-arms
//   - it cannot be shown again within 5 minutes of shownAt
export function bannerStep(state, input) {
  const s = {
    dismissed: !!(state && state.dismissed),
    shownAt: state && isPercent(state.shownAt) ? state.shownAt : null,
    visible: !!(state && state.visible),
  };
  const percent = input ? input.percent : null;
  const now = input ? Number(input.now) : NaN;
  const eligible = !!(input && input.live) && isPercent(percent);

  if (input && input.dismissAction && s.visible) {
    s.visible = false;
    s.dismissed = true;
  }
  if (!eligible) {
    s.visible = false;
    return { state: s, show: false };
  }
  if (s.dismissed) {
    if (percent < BANNER_REARM_BELOW) s.dismissed = false;
    return { state: s, show: false };
  }
  if (!s.visible && percent >= BANNER_SHOW_AT) {
    const cooled = s.shownAt === null || (Number.isFinite(now) && now - s.shownAt >= BANNER_COOLDOWN_MS);
    if (cooled) {
      s.visible = true;
      s.shownAt = Number.isFinite(now) ? now : s.shownAt;
    }
  }
  return { state: s, show: s.visible };
}
