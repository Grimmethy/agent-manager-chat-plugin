// A rate gate for an async refresh function (2026-11-02). The Chat switcher's refresh() makes two network
// calls and is triggered from many places (the 5 s timer, every pane change, every multi-chat render), and the
// plugin log showed GET /api/chat/terminal-sessions climbing from 6 to 22 requests per second in one burst.
//
// createRefreshGate(fn, opts) returns gated(). Rules:
//   - a call while a run is in flight returns THAT run's promise (no second run);
//   - a call less than minGapMs after the last run STARTED shares ONE trailing run, scheduled for the end of
//     the gap, however many calls arrive meanwhile;
//   - any other call runs at once, so the first click after a quiet spell is not delayed;
//   - a rejecting fn rejects the promise of that run only, and the gate keeps working.
// now/setTimer/clearTimer are injectable so tests run on a fake clock. Pure: no DOM, no fetch.
export function createRefreshGate(fn, { minGapMs = 1000, now = Date.now, setTimer = setTimeout } = {}) {
  let inFlight = null;
  let lastStart = null;
  let trailing = null;

  function run() {
    lastStart = now();
    const p = Promise.resolve().then(fn);
    inFlight = p;
    const clear = () => { if (inFlight === p) inFlight = null; };
    p.then(clear, clear);
    return p;
  }

  return function gated() {
    if (inFlight) return inFlight;
    if (trailing) return trailing;
    if (lastStart !== null && now() - lastStart < minGapMs) {
      const wait = Math.max(0, minGapMs - (now() - lastStart));
      trailing = new Promise((resolve, reject) => {
        setTimer(() => {
          trailing = null;
          run().then(resolve, reject);
        }, wait);
      });
      return trailing;
    }
    return run();
  };
}
