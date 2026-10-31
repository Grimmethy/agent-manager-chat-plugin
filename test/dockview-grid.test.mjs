// static/dockview-grid.js: the window.DockviewGrid adapter multi-chat.js needs. Three layers:
//   1. against a FAKE dockview-core (always runs): the calls the adapter makes and how it maps multi-chat's positions;
//   2. wiring (always runs): it is a classic script that parses, chat.html loads it between the vendored bundle and multi-chat.js, and every grid.<method> that
//      multi-chat.js calls exists on the adapter;
//   3. the REAL vendored bundle in headless Chromium (skipped, with the reason printed, when playwright-core or the browser is not installed).
// Run: node --test test/dockview-grid.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const ADAPTER = path.join(ROOT, 'static', 'dockview-grid.js');
const BUNDLE = path.join(ROOT, 'static', 'vendor', 'dockview.js');
const adapterSource = fs.readFileSync(ADAPTER, 'utf8');

// ---- layer 1: fake dockview-core -------------------------------------------------------------------------------------------------
function fakeLib() {
  const calls = { create: [], addPanel: [], listeners: [], disposed: 0 };
  const api = {
    addPanel(req) { calls.addPanel.push(req); return { id: req.id }; },
    onDidRemovePanel(listener) { calls.listeners.push(listener); return { dispose() { calls.listenerDisposed = true; } }; },
    dispose() { calls.disposed += 1; },
  };
  return { calls, api, lib: { createDockview(el, opts) { calls.create.push({ el, opts }); return api; } } };
}
function loadAdapter(lib) {
  const window = {};
  const context = vm.createContext({ window, document: { createElement: () => ({ style: {} }) }, globalThis: undefined });
  context.globalThis = lib === undefined ? {} : { 'dockview-core': lib };
  vm.runInContext(adapterSource, context, { filename: 'dockview-grid.js' });
  return window.DockviewGrid;
}
const pane = (id, direction) => ({ position: { direction }, component: { id, render() {} } });

test('without dockview-core the adapter defines nothing, so multi-chat.js keeps its own panes', () => {
  assert.equal(loadAdapter(undefined), undefined);
  assert.equal(loadAdapter({}), undefined, 'a library without createDockview is treated as absent');
});

test('the constructor calls createDockview(host, options) and subscribes with onDidRemovePanel as an event function', () => {
  const f = fakeLib();
  const Grid = loadAdapter(f.lib);
  const host = {};
  new Grid(host, {});
  assert.equal(f.calls.create.length, 1);
  assert.equal(f.calls.create[0].el, host);
  assert.equal(typeof f.calls.create[0].opts.createComponent, 'function');
  assert.equal(f.calls.listeners.length, 1);
});

test('addPane maps first / nextTo / below to dockview positions: nextTo is right of the pane added last, below is a root-level row', () => {
  const f = fakeLib();
  const Grid = loadAdapter(f.lib);
  const g = new Grid({}, {});
  ['first', 'nextTo', 'below', 'nextTo'].forEach((d, i) => g.addPane(pane('p' + i, d)));
  const positions = JSON.parse(JSON.stringify(f.calls.addPanel.map((r) => r.position ?? null)));
  assert.deepEqual(positions, [null, { referencePanel: 'p0', direction: 'right' }, { direction: 'below' }, { referencePanel: 'p2', direction: 'right' }]);
  assert.ok(f.calls.addPanel.every((r) => r.component === 'chat-pane' && r.title === r.id));
});

test('createComponent hands back a renderer whose init() calls the component render(el) with that panel element', () => {
  const f = fakeLib();
  const Grid = loadAdapter(f.lib);
  const g = new Grid({}, {});
  const seen = [];
  g.addPane({ position: { direction: 'first' }, component: { id: 'p0', render(el) { seen.push(el); } } });
  const renderer = f.calls.create[0].opts.createComponent({ id: 'p0', name: 'chat-pane' });
  assert.ok(renderer.element);
  renderer.init({});
  assert.deepEqual(seen, [renderer.element]);
});

test('closing a panel calls onClose callbacks with { component } for that pane; dispose silences them', () => {
  const f = fakeLib();
  const Grid = loadAdapter(f.lib);
  const g = new Grid({}, {});
  g.addPane(pane('p0', 'first'));
  g.addPane(pane('p1', 'nextTo'));
  const closed = [];
  g.onClose((p) => closed.push(p.component.id));
  f.calls.listeners[0]({ id: 'p1' });
  assert.deepEqual(closed, ['p1']);
  g.dispose();
  f.calls.listeners[0]({ id: 'p0' }); // dockview removing panels while it is being disposed
  assert.deepEqual(closed, ['p1'], 'no callback after dispose');
  assert.equal(f.calls.disposed, 1);
  g.dispose();
  assert.equal(f.calls.disposed, 1, 'dispose is idempotent');
});

test('addPane without a component id is refused', () => {
  const Grid = loadAdapter(fakeLib().lib);
  assert.throws(() => new Grid({}, {}).addPane({ position: { direction: 'first' }, component: {} }), /component\.id/);
});

// ---- layer 2: wiring ---------------------------------------------------------------------------------------------------------------
test('the adapter is a classic script: it parses with vm.Script and has no import or export', () => {
  assert.doesNotThrow(() => new vm.Script(adapterSource, { filename: 'dockview-grid.js' }));
  assert.ok(!/^\s*(import|export)\b/m.test(adapterSource), 'import/export is a SyntaxError in a classic <script>');
});

test('chat.html loads the vendored bundle, then the adapter, then the multi-chat module', () => {
  const html = fs.readFileSync(path.join(ROOT, 'templates', 'chat.html'), 'utf8');
  const a = html.indexOf('<script src="static/vendor/dockview.js"');
  const b = html.indexOf('<script src="static/dockview-grid.js"');
  const c = html.indexOf('src="static/multi-chat.js"');
  assert.ok(a > 0 && b > a && c > b, `order was bundle=${a} adapter=${b} multi-chat=${c}`);
  assert.ok(!/type="module"[^>]*dockview-grid/.test(html), 'the adapter must not be a module script');
});

test('every grid.<method> that multi-chat.js calls exists on the adapter', () => {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'multi-chat.js'), 'utf8');
  const used = [...new Set([...src.matchAll(/\bgrid\.(\w+)\(/g)].map((m) => m[1]))];
  assert.ok(used.includes('addPane') && used.includes('onClose'), `multi-chat.js calls ${used.join(', ')}`);
  const Grid = loadAdapter(fakeLib().lib);
  for (const name of used) assert.equal(typeof Grid.prototype[name], 'function', `multi-chat.js calls grid.${name}() but the adapter has no such method`);
  assert.match(src, /new window\.DockviewGrid\(hostEl, \{\}\)/);
});

test('multi-chat.js keeps the live grid and disposes it before it redraws and before it abandons it for the fallback strip', () => {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'multi-chat.js'), 'utf8');
  assert.match(src, /dockGrid = grid;/, 'the grid is remembered after it is built');
  assert.match(src, /function render\(\) \{\s*disposeDockGrid\(\);/, 'render() disposes the previous grid first');
  assert.match(src, /disposeDockGrid\(\);\s*if \(host\.firstChild\) host\.removeChild/, 'the abandoned-dockview path disposes it too');
});

// ---- layer 3: the real bundle in real Chromium ------------------------------------------------------------------------------------
const EXE = process.env.CHROMIUM_EXECUTABLE || path.join(os.homedir(), '.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell');
function loadPlaywright() {
  for (const base of [process.env.PLAYWRIGHT_CORE_FROM, '/media/model-cache/github/uxforge', ROOT].filter(Boolean)) {
    try { return createRequire(path.join(base, 'package.json'))('playwright-core'); } catch { /* try the next place */ }
  }
  return null;
}
const playwright = loadPlaywright();
const realSkip = !playwright ? 'playwright-core is not installed' : !fs.existsSync(EXE) ? `no headless Chromium at ${EXE} (set CHROMIUM_EXECUTABLE)` : false;
if (realSkip) console.log(`# real-browser dockview tests skipped: ${realSkip}`);

async function withPage(fn) {
  const browser = await playwright.chromium.launch({ headless: true, executablePath: EXE });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.setContent('<div id="h" style="width:900px;height:600px"></div>');
    await page.addScriptTag({ path: BUNDLE });
    await page.addScriptTag({ path: ADAPTER });
    await fn(page, errors);
    assert.deepEqual(errors, [], 'no page errors');
  } finally {
    await browser.close();
  }
}
const mount = (positions) => `(() => {
  const g = window.__g = new window.DockviewGrid(document.getElementById('h'), {});
  window.__rendered = []; window.__closed = [];
  ${JSON.stringify(positions)}.forEach((d, i) => g.addPane({ position: { direction: d }, component: { id: 'p' + i, render(el) { el.textContent = 'pane-p' + i; window.__rendered.push('p' + i); } } }));
  g.onClose((p) => window.__closed.push(p.component.id));
})()`;

test('real bundle: window.DockviewGrid is a function and every pane renders into the page', { skip: realSkip }, async () => {
  await withPage(async (page) => {
    assert.equal(await page.evaluate(() => typeof window.DockviewGrid), 'function');
    await page.evaluate(mount(['first', 'nextTo']));
    assert.deepEqual(await page.evaluate(() => window.__rendered), ['p0', 'p1']);
    assert.equal(await page.evaluate(() => document.querySelectorAll('.dv-groupview').length), 2);
    assert.ok((await page.evaluate(() => document.body.textContent)).includes('pane-p1'));
  });
});

test('real bundle: the 2x2 preset gives a|b over c|d, and 2col puts two panes side by side', { skip: realSkip }, async () => {
  await withPage(async (page) => {
    await page.evaluate(mount(['first', 'nextTo', 'below', 'nextTo']));
    const r = await page.evaluate(() => {
      const out = {};
      document.querySelectorAll('.dv-groupview').forEach((el) => { const b = el.getBoundingClientRect(); out[el.textContent.trim().replace('pane-', '').slice(0, 2)] = [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; });
      return out;
    });
    const [a, b, c, d] = [r.p0, r.p1, r.p2, r.p3];
    assert.ok(b[0] > a[0] && b[1] === a[1], 'p1 is right of p0 on the same row');
    assert.ok(c[1] > a[1] && c[0] === a[0], 'p2 is under p0 at the left');
    assert.ok(d[0] > c[0] && d[1] === c[1], 'p3 is right of p2 on the same row');
    const cells = [a, b, c, d];
    assert.ok(cells.every((x) => Math.abs(x[2] - a[2]) <= 2 && Math.abs(x[3] - a[3]) <= 2), `four equal cells, got ${JSON.stringify(cells)}`);
  });
});

test('real bundle: closing a pane through its tab close button calls onClose with that pane id', { skip: realSkip }, async () => {
  await withPage(async (page) => {
    await page.evaluate(mount(['first', 'nextTo']));
    const closeButtons = page.locator('.dv-default-tab-action');
    assert.equal(await closeButtons.count(), 2, 'each tab has a close action');
    await closeButtons.nth(1).click();
    assert.deepEqual(await page.evaluate(() => window.__closed), ['p1']);
    assert.equal(await page.evaluate(() => document.querySelectorAll('.dv-groupview').length), 1);
  });
});

test('real bundle: dispose() removes the panes without firing onClose', { skip: realSkip }, async () => {
  await withPage(async (page) => {
    await page.evaluate(mount(['first', 'nextTo']));
    await page.evaluate(() => window.__g.dispose());
    assert.deepEqual(await page.evaluate(() => window.__closed), []);
  });
});
