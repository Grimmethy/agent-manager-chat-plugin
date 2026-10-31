// Defines window.DockviewGrid, the small adapter static/multi-chat.js tryRenderDockview() looks for, on top of the vendored dockview-core (static/vendor/dockview.js).
//
// A PLAIN classic <script> (no import/export): templates/chat.html loads it after static/vendor/dockview.js (whose UMD build sets globalThis['dockview-core'], with a hyphen)
// and before the multi-chat.js module, which runs deferred. If dockview-core is not there the constructor is simply not defined and multi-chat.js keeps its own flexbox panes.
//
// The contract multi-chat.js uses (pinned by test/dockview-grid.test.mjs):
//   const grid = new window.DockviewGrid(hostEl, {})
//   grid.addPane({ position: { direction: 'first' | 'nextTo' | 'below' }, component: { id, render(el) } })
//   grid.onClose((pane) => ...)   pane = { component } of the pane that was removed (the tab's close button, or removal through the API)
//   grid.dispose()                multi-chat.js calls it before it redraws, so an old grid stops listening
// dockview-core calls used (all present in 8.4.1): createDockview(element, { createComponent, className }) -> api; api.addPanel({ id, component, title, position });
// api.onDidRemovePanel(listener) (an event FUNCTION you call with a listener); api.dispose().
(function () {
  'use strict';
  var lib = typeof globalThis !== 'undefined' ? globalThis['dockview-core'] : undefined;
  if (!lib || typeof lib.createDockview !== 'function') return;

  var PANEL_COMPONENT = 'chat-pane';

  function DockviewGrid(hostEl, options) {
    var self = this;
    this._components = {}; // panel id -> the component object multi-chat.js gave addPane
    this._order = [];      // panel ids in the order they were added
    this._closeCallbacks = [];
    this._disposed = false;
    this._api = lib.createDockview(hostEl, {
      className: (options && options.className) || 'dockview-theme-dark',
      createComponent: function (opts) {
        var element = document.createElement('div');
        element.style.height = '100%';
        return {
          element: element,
          init: function () {
            var component = self._components[opts.id];
            if (component && typeof component.render === 'function') component.render(element);
          },
          dispose: function () {},
        };
      },
    });
    this._removeSubscription = this._api.onDidRemovePanel(function (panel) {
      if (self._disposed) return; // dispose() removes every panel; that is not a user closing one
      var id = panel && panel.id;
      var component = self._components[id] || { id: id };
      delete self._components[id];
      self._order = self._order.filter(function (x) { return x !== id; });
      self._closeCallbacks.slice().forEach(function (cb) { cb({ component: component }); });
    });
  }

  // 'first' = no placement; 'nextTo' = right of the pane added last; 'below' = a new row along the bottom of the whole grid (a position with no referencePanel
  // is placed at the root, so the 2x2 preset gives a|b over c|d; splitting under pane 1 alone would leave pane 2 as a full-height column).
  DockviewGrid.prototype._placement = function (direction) {
    if (direction === 'nextTo' && this._order.length) return { referencePanel: this._order[this._order.length - 1], direction: 'right' };
    if (direction === 'below' && this._order.length) return { direction: 'below' };
    return undefined;
  };

  DockviewGrid.prototype.addPane = function (pane) {
    var component = pane && pane.component;
    if (!component || typeof component.id !== 'string' || !component.id) throw new Error('addPane needs component.id');
    var position = this._placement(pane.position && pane.position.direction);
    this._components[component.id] = component;
    var request = { id: component.id, component: PANEL_COMPONENT, title: component.title || component.id };
    if (position) request.position = position;
    this._api.addPanel(request);
    this._order.push(component.id);
    return component.id;
  };

  DockviewGrid.prototype.onClose = function (callback) {
    if (typeof callback === 'function') this._closeCallbacks.push(callback);
  };

  DockviewGrid.prototype.dispose = function () {
    if (this._disposed) return;
    this._disposed = true;
    this._closeCallbacks = [];
    try { if (this._removeSubscription && this._removeSubscription.dispose) this._removeSubscription.dispose(); } catch (e) { /* already gone */ }
    try { this._api.dispose(); } catch (e) { /* the host element may already be detached */ }
  };

  window.DockviewGrid = DockviewGrid;
})();
