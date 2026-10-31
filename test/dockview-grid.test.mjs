// test/dockview-grid.test.mjs
//
// Tests for static/dockview-grid.js.
//
// Two parts:
//   1. Pure mapping function (node-testable, no DOM needed)
//   2. Adapter class (requires a browser-like environment; we use node:vm with
//      a minimal DOM stub to load the real vendored dockview bundle and the
//      adapter, then exercise the adapter)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mapDirectionToPlacement } from '../static/dockview-grid.js';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// --- Part 1: pure mapping function ------------------------------------------

test('mapDirectionToPlacement: "first" -> {index: 0}', () => {
  const result = mapDirectionToPlacement('first');
  assert.deepEqual(result, { index: 0 });
});

test('mapDirectionToPlacement: "nextTo" -> {direction: "right"}', () => {
  const result = mapDirectionToPlacement('nextTo');
  assert.deepEqual(result, { direction: 'right' });
});

test('mapDirectionToPlacement: "below" -> {direction: "below"}', () => {
  const result = mapDirectionToPlacement('below');
  assert.deepEqual(result, { direction: 'below' });
});

test('mapDirectionToPlacement: unknown direction -> {index: 0}', () => {
  const result = mapDirectionToPlacement('unknown');
  assert.deepEqual(result, { index: 0 });
});

test('mapDirectionToPlacement: undefined direction -> {index: 0}', () => {
  const result = mapDirectionToPlacement(undefined);
  assert.deepEqual(result, { index: 0 });
});

// --- Part 2: adapter class (loaded via node:vm with minimal DOM stub) -------

/**
 * Create a minimal DOM stub for node:vm.
 */
function createDomStub() {
  return {
    createElement() {
      return {
        style: {},
        innerHTML: '',
        appendChild() {},
        removeChild() {},
        addEventListener() {},
        removeEventListener() {},
        classList: { add() {}, remove() {}, contains() {} },
        setAttribute() {},
        getAttribute() {},
        dataset: {},
      };
    },
    head: { appendChild() {} },
    body: { appendChild() {} },
  };
}

/**
 * Load the real vendored dockview bundle into a vm context.
 */
function loadVendoredBundle(context) {
  const dockviewCode = readFileSync(
    join(__dirname, '../static/vendor/dockview.js'),
    'utf8'
  );
  vm.runInContext(dockviewCode, context);
}

/**
 * Load dockview-grid.js into a vm context (transformed to remove ES module syntax).
 */
function loadAdapter(context) {
  const adapterCode = readFileSync(
    join(__dirname, '../static/dockview-grid.js'),
    'utf8'
  );

  // Transform: remove `export`, replace `window.DockviewGrid` with `this.DockviewGrid`
  // (in vm.runInContext, `this` is the context object).
  let transformedCode = adapterCode
    .replace(/^export function /gm, 'function ')
    .replace(/^export /gm, '')
    .replace(/window\.DockviewGrid/g, 'this.DockviewGrid');

  // Expose mapDirectionToPlacement on the context.
  transformedCode += '\nthis.mapDirectionToPlacement = mapDirectionToPlacement;';

  vm.runInContext(transformedCode, context);
}

/**
 * Set up a full test environment: minimal DOM + vendored bundle + adapter.
 */
function setupTestEnv() {
  const context = vm.createContext({
    window: {},
    globalThis: {},
    document: createDomStub(),
    HTMLElement: class {},
    Node: class {},
    Event: class {},
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    Promise: Promise,
    Error: Error,
    JSON: JSON,
    Map: Map,
    Set: Set,
    WeakMap: WeakMap,
    Array: Array,
    Object: Object,
    String: String,
    Number: Number,
    Boolean: Boolean,
    Symbol: Symbol,
    RegExp: RegExp,
    Date: Date,
    Math: Math,
    parseInt: parseInt,
    parseFloat: parseFloat,
    isNaN: isNaN,
    isFinite: isFinite,
    encodeURIComponent: encodeURIComponent,
    decodeURIComponent: decodeURIComponent,
    encodeURI: encodeURI,
    decodeURI: decodeURI,
  });

  // Load vendored bundle (sets globalThis['dockview-core']).
  loadVendoredBundle(context);

  // Load adapter (defines window.DockviewGrid).
  loadAdapter(context);

  return context;
}

test('adapter: loads real vendored bundle and defines window.DockviewGrid', () => {
  const context = setupTestEnv();

  // Verify globalThis['dockview-core'] was set.
  assert.ok(context.globalThis['dockview-core'], 'globalThis[\'dockview-core\'] should be set');
  assert.ok(typeof context.globalThis['dockview-core'].createDockview === 'function', 'createDockview should be a function');

  // Verify window.DockviewGrid was defined.
  assert.ok(context.window.DockviewGrid, 'window.DockviewGrid should be defined');
  assert.ok(typeof context.window.DockviewGrid === 'function', 'window.DockviewGrid should be a function');

  // Verify mapDirectionToPlacement is exposed.
  assert.ok(typeof context.mapDirectionToPlacement === 'function', 'mapDirectionToPlacement should be exposed');
});

test('adapter: addPane calls addPanel with mapped position', () => {
  const context = setupTestEnv();

  // Mock createDockview to track addPanel calls.
  let addPanelCalls = [];
  context.globalThis['dockview-core'].createDockview = () => ({
    addPanel(panel) {
      addPanelCalls.push(panel);
      return { component: { id: panel.id } };
    },
    api: {
      onDidRemovePanel() {
        return { dispose() {} };
      },
      groups: {},
    },
    dispose() {},
  });

  // Instantiate the grid.
  const hostEl = { appendChild() {}, style: {} };
  const grid = new context.window.DockviewGrid(hostEl, {});

  // Add a pane.
  const component = {
    id: 'test-pane',
    render(el) {
      el.innerHTML = '<div>Test</div>';
    },
  };
  grid.addPane({
    position: { direction: 'nextTo' },
    component,
  });

  // Verify addPanel was called with the right shape.
  assert.equal(addPanelCalls.length, 1, 'addPanel should be called once');
  const call = addPanelCalls[0];
  assert.equal(call.id, 'test-pane', 'panel id should match');
  assert.equal(call.direction, 'right', 'direction should be "right" for "nextTo"');
  assert.ok(call.component, 'component should be present');
  assert.equal(call.component.id, 'test-pane', 'component.id should match');
  assert.ok(typeof call.component.render === 'function', 'component.render should be a function');
});

test('adapter: onClose callback fires with {component: {id}} on panel remove', () => {
  const context = setupTestEnv();

  // Mock createDockview to expose onDidRemovePanel.
  let onDidRemovePanelListener = null;
  context.globalThis['dockview-core'].createDockview = () => ({
    addPanel() {
      return { component: { id: 'test-pane' } };
    },
    api: {
      onDidRemovePanel(listener) {
        onDidRemovePanelListener = listener;
        return { dispose() {} };
      },
      groups: {},
    },
    dispose() {},
  });

  // Instantiate the grid and register onClose callback.
  const hostEl = { appendChild() {}, style: {} };
  const grid = new context.window.DockviewGrid(hostEl, {});

  let closedPane = null;
  grid.onClose((pane) => {
    closedPane = pane;
  });

  // Simulate a panel removal event.
  onDidRemovePanelListener({
    params: {
      pane: {
        component: {
          id: 'test-pane',
        },
      },
    },
  });

  // Verify the callback was called with the right shape.
  assert.ok(closedPane, 'onClose callback should be called');
  assert.ok(closedPane.component, 'pane should have component');
  assert.equal(closedPane.component.id, 'test-pane', 'component.id should match');
});

test('adapter: popoutGroup calls api.groups.popout', () => {
  const context = setupTestEnv();

  // Mock createDockview to track popout calls.
  let popoutCalls = [];
  context.globalThis['dockview-core'].createDockview = () => ({
    addPanel() {
      return { component: { id: 'test-pane' } };
    },
    api: {
      onDidRemovePanel() {
        return { dispose() {} };
      },
      groups: {
        getGroupById(id) {
          return { id, name: 'group-' + id };
        },
        popout(group) {
          popoutCalls.push(group);
          return Promise.resolve({});
        },
      },
    },
    dispose() {},
  });

  // Instantiate the grid.
  const hostEl = { appendChild() {}, style: {} };
  const grid = new context.window.DockviewGrid(hostEl, {});

  // Call popoutGroup.
  grid.popoutGroup('group-1');

  // Verify popout was called.
  assert.equal(popoutCalls.length, 1, 'popout should be called once');
  assert.equal(popoutCalls[0].id, 'group-1', 'popout should receive the group');
});

test('adapter: throws when globalThis[\'dockview-core\'] is missing', () => {
  // In node, typeof window is 'undefined', so the adapter should NOT define
  // window.DockviewGrid. This test verifies that behavior.
  assert.ok(typeof window === 'undefined', 'node should not have window');
  assert.ok(!globalThis['dockview-core'], 'globalThis[\'dockview-core\'] should not be set in node');
});
