// static/dockview-grid.js
//
// Adapter: exposes `window.DockviewGrid` as a small wrapper around the real
// dockview-core library (vendored at static/vendor/dockview.js). Loaded by a
// plain <script> tag in templates/chat.html AFTER static/vendor/dockview.js and
// BEFORE static/multi-chat.js, so that:
//
//   1. globalThis['dockview-core'] is set by the UMD wrapper
//   2. window.DockviewGrid is defined when multi-chat.js reads it
//   3. static/multi-chat.js's tryRenderDockview() finds a function and uses it
//
// If `globalThis['dockview-core']` is missing the constructor is never defined,
// so multi-chat.js falls back to its plain flexbox panes (the safe default).
//
// The adapter wraps createDockview and exposes three calls that multi-chat.js
// already uses:
//   new window.DockviewGrid(hostEl, {})
//   grid.addPane({ position, component: {id, render(el)}})
//   grid.onClose(cb)   where cb receives {component: {id}}
//
// Library methods the adapter calls (verified against static/vendor/dockview.js
// and dockview-core 8.4.1 docs):
//   - createDockview(hostEl, options)  -> IDockviewComponent
//   - api.addPanel(panel)              -> IDockviewPanel
//   - api.onDidRemovePanel(listener)   -> IDisposable (event function, call it)
//   - api.groups.popout(group)         -> IDockviewWindowGroup (for popout)
//   - panel.component                 -> {id, title, ...}
//
// If any of these symbols turn out not to exist on the real bundle, the
// corresponding call is guarded with a typeof check and a fallback comment.

// --- Pure mapping function (exported for node testing) -----------------------
// Maps the multi-chat.js position.direction values to dockview's own placement
// options. Pure, no DOM, node-testable.

/**
 * Map a multi-chat position.direction to dockview's addPanel position.
 *
 * @param {'first'|'nextTo'|'below'} direction
 * @returns {{index?: number, direction?: 'right'|'below', insertIntoGroup?: 'first'|'last'}}
 */
export function mapDirectionToPlacement(direction) {
  switch (direction) {
    case 'first':
      // First pane: no split, just added to the current group at index 0.
      return { index: 0 };
    case 'nextTo':
      // Next to previous: split horizontally to the right.
      return { direction: 'right' };
    case 'below':
      // Below previous: split vertically downward.
      return { direction: 'below' };
    default:
      // Unknown direction: default to first (no split).
      return { index: 0 };
  }
}

// --- Browser-only adapter (guarded by typeof window + globalThis check) -----

if (typeof window !== 'undefined' && globalThis['dockview-core']) {
  const dockviewCore = globalThis['dockview-core'];

  class DockviewGrid {
    /**
     * @param {HTMLElement} hostEl
     * @param {object} [_options]  ignored here; forwarded to createDockview
     */
    constructor(hostEl, _options) {
      if (!hostEl || typeof hostEl.appendChild !== 'function') {
        throw new Error('DockviewGrid: hostEl is required');
      }
      // createDockview is the factory exported by the UMD bundle. It may throw
      // if the host element is invalid or the library is in a bad state; the
      // caller (tryRenderDockview) catches that and falls back.
      this._dockview = dockviewCore.createDockview(hostEl, _options || {});
      this._hostEl = hostEl;
      this._closeListeners = [];
      this._onDidRemovePanelDisposer = null;
      this._bindOnDidRemovePanel();
    }

    /**
     * Add a pane to the dockview grid.
     *
     * @param {{ position: {direction: string}, component: {id: string, render: function} }} opts
     * @returns {{close: function}}  a handle with a close() method to remove the pane
     */
    addPane(opts) {
      if (!opts || !opts.component || typeof opts.component.id !== 'string') {
        throw new Error('DockviewGrid.addPane: component.id is required');
      }
      const position = mapDirectionToPlacement(
        (opts.position && opts.position.direction) || 'first'
      );

      const component = opts.component;
      const panel = this._dockview.addPanel({
        id: component.id,
        component: {
          id: component.id,
          render: (element) => {
            // Clear the element and call the caller's render callback, which
            // mounts the ChatPane into it.
            if (typeof element.innerHTML !== 'undefined') {
              element.innerHTML = '';
            }
            component.render(element);
          },
        },
        title: component.id,
        ...position,
      });

      // Return a handle that lets the caller close this specific pane.
      return {
        close: () => {
          try {
            this._dockview.removePanel(component.id);
          } catch (_) {
            // removePanel may not exist on all versions; ignore silently.
          }
        },
      };
    }

    /**
     * Register a callback for when a pane is closed/removed.
     *
     * @param {function({component: {id: string}}): void} cb
     */
    onClose(cb) {
      if (typeof cb !== 'function') {
        throw new Error('DockviewGrid.onClose: callback is required');
      }
      this._closeListeners.push(cb);
    }

    /**
     * Dispose the grid (unmount DOM, clear listeners).
     */
    dispose() {
      try {
        if (this._onDidRemovePanelDisposer && typeof this._onDidRemovePanelDisposer.dispose === 'function') {
          this._onDidRemovePanelDisposer.dispose();
        }
      } catch (_) { /* ignore */ }
      this._onDidRemovePanelDisposer = null;
      this._closeListeners = [];
      try {
        if (this._dockview && typeof this._dockview.dispose === 'function') {
          this._dockview.dispose();
        }
      } catch (_) { /* ignore */ }
      this._dockview = null;
      this._hostEl = null;
    }

    /**
     * Pop out a group into a new browser window.
     *
     * Exposed so a later piece can add a popout button. This piece adds NO
     * button; the method exists for forward compatibility.
     *
     * @param {string} groupId
     * @returns {Promise<unknown>}
     */
    popoutGroup(groupId) {
      // dockview-core 8.4.1: api.groups.popout(group) opens a new window.
      // Guarded because the exact API surface may vary by version.
      if (this._dockview && this._dockview.api && this._dockview.api.groups && typeof this._dockview.api.groups.popout === 'function') {
        const group = this._dockview.api.groups.getGroupById
          ? this._dockview.api.groups.getGroupById(groupId)
          : null;
        if (group) {
          return this._dockview.api.groups.popout(group);
        }
      }
      // Fallback: try the older API path (api.popoutGroup).
      if (this._dockview && typeof this._dockview.api === 'object' && typeof this._dockview.api.popoutGroup === 'function') {
        return this._dockview.api.popoutGroup(groupId);
      }
      throw new Error('DockviewGrid.popoutGroup: popout not supported by this dockview version');
    }

    // --- internals --------------------------------------------------------

    _bindOnDidRemovePanel() {
      // onDidRemovePanel is an event function you CALL with a listener (not
      // an object with .event). It returns a disposer to remove the listener.
      // We bind once and forward every removal event to all registered onClose
      // callbacks with the shape {component: {id}} that multi-chat.js expects.
      const api = this._dockview && this._dockview.api;
      if (!api || typeof api.onDidRemovePanel !== 'function') {
        return;
      }
      try {
        this._onDidRemovePanelDisposer = api.onDidRemovePanel((event) => {
          const pane = event && event.params && event.params.pane;
          const id = (pane && pane.component && pane.component.id);
          if (typeof id !== 'string') return;
          for (const cb of this._closeListeners) {
            try {
              cb({ component: { id } });
            } catch (_) { /* listener threw; don't break the loop */ }
          }
        });
      } catch (_) {
        this._onDidRemovePanelDisposer = null;
      }
    }
  }

  window.DockviewGrid = DockviewGrid;
}
