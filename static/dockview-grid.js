// dockview-grid.js -- thin browser adapter over the vendored dockview-core.
//
// The UMD build in static/vendor/dockview.js (dockview-core 8.4.1) sets
// `globalThis['dockview-core']` and exports `createDockview`. This file wraps
// that in a small `DockviewGrid` class -- the constructor name `static/multi-chat.js`
// already looks for (`new window.DockviewGrid(hostEl, {})`), whose `addPane` /
// `onClose` usage this adapter matches:
//
//   grid = new DockviewGrid(hostEl, options)
//   pane = grid.addPane({ position, component })   // dockview-core pane options
//   grid.onClose((pane) => {...})                  // grid-level close handler
//   grid.popout(pane)                              // move a pane into a new window
//
// No DOM assumptions at load time: under Node the file only exposes the pure
// `mapDirection` helper (via the module.exports guard below) and the class.

(function (globalScope) {
  'use strict';

  // --- mapDirection -------------------------------------------------------
  // Pure direction normaliser: maps dockview-side direction tokens to an
  // { axis, side } pair. No `this`, no DOM, no globals other than RangeError,
  // so it is trivially unit-testable in Node.
  var DIRECTION_AXES = {
    top: { axis: 'row', side: 'start' },
    bottom: { axis: 'row', side: 'end' },
    left: { axis: 'col', side: 'start' },
    right: { axis: 'col', side: 'end' }
  };

  function mapDirection(dir) {
    var mapped = Object.prototype.hasOwnProperty.call(DIRECTION_AXES, dir)
      ? DIRECTION_AXES[dir]
      : null;
    if (!mapped) {
      throw new RangeError('mapDirection: unknown direction ' + JSON.stringify(dir));
    }
    return mapped;
  }

  // --- DockviewGrid --------------------------------------------------------
  function DockviewGrid(hostEl, options) {
    if (!globalScope['dockview-core']) {
      throw new Error(
        "dockview-core global not found -- load static/vendor/dockview.js " +
        'before constructing DockviewGrid'
      );
    }
    if (!hostEl) {
      throw new TypeError('DockviewGrid: a host element is required');
    }
    this._el = hostEl;
    this._dv = globalScope['dockview-core'].createDockview(hostEl, options || {});
  }

  // Add a pane, passing dockview-core pane options through unchanged
  // ({ position, component, id, title, reference, ... }). Returns the pane.
  DockviewGrid.prototype.addPane = function (params) {
    return this._dv.addPane(params);
  };

  // Register a grid-level close handler; the callback receives the pane
  // (whose .component.id is the stable key multi-chat.js maps back on).
  DockviewGrid.prototype.onClose = function (callback) {
    return this._dv.onClose(callback);
  };

  // Pop a pane out into its own browser window: opens a same-origin
  // about:blank window (which shares this global object, so the dockview-core
  // global is still visible), builds a fresh DockviewGrid in it, re-adds the
  // pane's component there, and closes the pane in the original grid.
  // Returns { window, grid } or null if the popup was blocked.
  DockviewGrid.prototype.popout = function (pane) {
    var win = globalScope.window;
    if (typeof win === 'undefined' || typeof win.open !== 'function') {
      throw new Error('DockviewGrid.popout requires a browser window');
    }
    var newWin = win.open('', '_blank', 'width=800,height=600');
    if (!newWin) return null; // popup blocked

    var doc = newWin.document;
    if (doc.body && doc.body.style) doc.body.style.margin = '0';
    var hostEl = doc.createElement('div');
    hostEl.style.width = '100%';
    hostEl.style.height = '100%';
    doc.body.appendChild(hostEl);

    var newGrid = new DockviewGrid(hostEl, {});
    var component = pane && pane.component ? pane.component : { id: pane && pane.id };
    newGrid.addPane({ position: { direction: 'first' }, component: component });

    if (pane && typeof pane.close === 'function') pane.close();
    return { window: newWin, grid: newGrid };
  };

  // --- namespace exports ---------------------------------------------------
  if (globalScope && typeof globalScope.window !== 'undefined') {
    globalScope.window.DockviewGrid = DockviewGrid;
  }
  globalScope.mapDirection = mapDirection;

  // Node test hook: plain browser script above, no require/import.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { mapDirection: mapDirection, DockviewGrid: DockviewGrid };
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
