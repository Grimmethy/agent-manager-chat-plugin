// Multi-chat layout: several ChatPane instances (static/chat.js) on one page.
//
// Two sections, mirroring static/sessions-view.js:
//   1. TOP: pure, DOM-free, node-testable named exports over the layout state
//      { preset: '1' | '2col' | '2x2', panes: [sessionIds in recency order] }
//      where a `null` pane slot is an empty placeholder. Unit-testable with
//      `node --test` -- no window/document/fetch in this section.
//   2. BOTTOM: browser-only adapter (behind a typeof window/document guard so a
//      node import stays safe). Sets window.__CHAT_MULTI_PANES__ (chat.js's
//      mountChat() bails out when it is set), imports ChatPane from './chat.js',
//      and renders one ChatPane per open pane -- via window.DockviewGrid when
//      present, else a plain CSS flexbox split. The dashboard keeps working
//      either way.
//
// Hard rule: closing a pane UNMOUNTS ITS DOM ONLY. It never calls
// POST api/chat/<id>/close -- the session stays alive on the server; only the
// grid slot is removed.

const PRESETS = ['1', '2col', '2x2'];

function defaultState() {
  return { preset: '1', panes: [null] };
}

function openIds(state) {
  const panes = state && Array.isArray(state.panes) ? state.panes : [];
  return panes.filter((p) => typeof p === 'string' && p.length > 0);
}

// Apply a grid preset to a copy of `state`.
//   '1'    -> keep only the most recent open pane (null slots when there is none)
//   '2col' -> two slots, '2x2' -> four slots; slots without an open pane are null
// Throws RangeError on an unknown preset.
export function applyPreset(state, preset) {
  if (!PRESETS.includes(preset)) {
    throw new RangeError('applyPreset: unknown preset ' + JSON.stringify(preset) + ' (expected one of ' + PRESETS.join(', ') + ')');
  }
  const slots = preset === '2col' ? 2 : preset === '2x2' ? 4 : 1;
  const ids = openIds(state);
  const panes = ids.slice(0, slots);
  while (panes.length < slots) panes.push(null);
  return { preset, panes };
}

// Remove the pane with session id `id` from the grid (pure: it never touches the
// network -- the caller unmounts the pane's DOM). Returns
//   { state: <new state, same preset, id removed>, nextFocus: <most recent
//     remaining open pane id, or null when none remains> }
// If `id` is not present, state is returned unchanged (a fresh copy) and
// nextFocus is null.
export function closePane(state, id) {
  const src = state && Array.isArray(state.panes) ? state : { preset: '1', panes: [null] };
  const present = src.panes.includes(id);
  const panes = src.panes.filter((p) => p !== id);
  const remaining = panes.filter((p) => typeof p === 'string' && p.length > 0);
  return {
    state: { preset: src.preset, panes: panes.length ? panes : [null] },
    nextFocus: present && remaining.length ? remaining[0] : present ? null : null,
  };
}

export function serialize(state) {
  return JSON.stringify(state);
}

// Parse and validate a saved layout. ANY malformed shape (not an object, bad
// preset, panes not an array, non-string/null entries, empty panes) falls back
// to the default single-pane layout -- a stored layout must never break the page.
export function deserialize(json) {
  let parsed;
  try {
    parsed = JSON.parse(json);
  } catch (e) {
    return defaultState();
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return defaultState();
  if (!PRESETS.includes(parsed.preset)) return defaultState();
  if (!Array.isArray(parsed.panes) || parsed.panes.length === 0) return defaultState();
  for (const p of parsed.panes) {
    if (p !== null && (typeof p !== 'string' || p.length === 0)) return defaultState();
  }
  return { preset: parsed.preset, panes: [...parsed.panes] };
}

// Mount pool: to keep memory bounded with many restored panes, only the active
// pane plus the two most recent OTHER open panes stay mounted; the rest render
// as "open" placeholders that mount on demand. Returns a Set of pane ids.
export function mountedSet(state, activeId) {
  const ids = openIds(state);
  const set = new Set();
  if (typeof activeId === 'string' && activeId.length > 0 && ids.includes(activeId)) {
    set.add(activeId);
  }
  for (const id of ids) {
    if (set.size >= 3) break;
    if (id !== activeId) set.add(id);
  }
  return set;
}

// Most recent open pane id, or null when the layout has no open pane yet.
export function firstOpenPane(state) {
  const ids = openIds(state);
  return ids.length ? ids[0] : null;
}

// Phone-width rule (HUB0010 4/10): below `breakpoint` px the workspace shows one pane, the
// active chat (the first open pane when activeId is null or not open; none when nothing is
// open). At or above it the same state object comes back unchanged. Never mutates `state`.
export function layoutForWidth(state, activeId, width, breakpoint = 760) {
  if (!(width < breakpoint)) return state;
  const ids = openIds(state);
  const keep = typeof activeId === 'string' && ids.includes(activeId) ? activeId : ids[0];
  return { preset: '1', panes: keep === undefined ? [] : [keep] };
}

// ============================================================================
// Browser-only adapter. Node imports of this module stop at the guard above.
// ============================================================================
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const STORAGE_KEY = 'chat-workspace-layout';
  const MOBILE_MAX_WIDTH = 760;

  // Must be set BEFORE './chat.js' is imported: its mountChat() early-returns
  // while this flag is set, so the single-pane mount path never double-mounts.
  window.__CHAT_MULTI_PANES__ = true;

  function saveLayout(state) {
    try {
      window.localStorage.setItem(STORAGE_KEY, serialize(state));
    } catch (e) { /* storage disabled/unavailable: the layout just won't persist */ }
  }

  function restoreLayout() {
    let raw = null;
    try {
      raw = window.localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      raw = null;
    }
    return raw == null ? defaultState() : deserialize(raw);
  }

  function injectStyles() {
    if (document.getElementById('multi-chat-style')) return;
    const style = document.createElement('style');
    style.id = 'multi-chat-style';
    style.textContent =
      '.multi-chat-toolbar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:8px 12px; border-bottom:1px solid var(--border,#ddd); }' +
      '.multi-chat-toolbar .multi-chat-switcher { flex:1; min-width:0; }' +
      '.multi-chat-host { flex:1; min-height:0; overflow:auto; position:relative; }' +
      '.multi-chat-flex { display:flex; flex-wrap:wrap; gap:8px; padding:8px; align-content:flex-start; }' +
      '.multi-chat-pane, .multi-chat-placeholder { flex:0 0 calc(50% - 8px); min-height:280px; display:flex; flex-direction:column; }' +
      '.multi-chat-pane.wide, .multi-chat-placeholder.wide { flex-basis:100%; }' +
      '.multi-chat-pane-head { display:flex; align-items:center; justify-content:flex-end; gap:8px; padding:4px 8px 0; }' +
      '.multi-chat-pane-body { flex:1; min-height:0; }' +
      '.multi-chat-placeholder { border:2px dashed var(--border,#ccc); border-radius:8px; align-items:center; justify-content:center; background:transparent; }' +
      '.multi-chat-mobile-bar { display:flex; align-items:center; gap:8px; padding:8px 12px; }' +
      '.multi-chat-mobile-bar select { flex:1; min-width:0; }';
    document.head.appendChild(style);
  }

  function boot() {
    const root =
      document.getElementById('multi-chat-root') ||
      document.getElementById('chat-panel') ||
      document.body;

    injectStyles();
    let state = restoreLayout();
    let activeId = firstOpenPane(state);

    const toolbar = document.createElement('div');
    toolbar.className = 'multi-chat-toolbar';
    const host = document.createElement('div');
    host.className = 'multi-chat-host';
    root.appendChild(toolbar);
    root.appendChild(host);

    // --- toolbar: preset buttons (existing .secondary class) ----------------
    const presetButtons = {};
    const PRESET_LABELS = { '1': 'Single', '2col': 'Two columns', '2x2': 'Grid 2x2' };
    for (const preset of PRESETS) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'secondary multi-chat-preset-btn';
      btn.textContent = PRESET_LABELS[preset];
      btn.setAttribute('aria-pressed', 'false');
      btn.onclick = () => {
        state = applyPreset(state, preset);
        activeId = firstOpenPane(state) || activeId;
        saveLayout(state);
        render();
      };
      toolbar.appendChild(btn);
      presetButtons[preset] = btn;
    }
    function markPreset() {
      for (const p of PRESETS) presetButtons[p].setAttribute('aria-pressed', String(state.preset === p));
    }

    // --- one ChatPane inside a framed slot (close = DOM unmount, no API) ----
    let ChatPane = null;
    function mountPane(id, bodyEl, isNew) {
      const pane = new ChatPane(bodyEl, {});
      const opener = isNew ? pane.startNew() : pane.loadSession(id);
      // After startNew the pane knows its fresh session id -- record it so the
      // saved layout stores a real id instead of a placeholder.
      opener.then(() => {
        if (isNew && pane.session && pane.session.id) {
          const i = state.panes.indexOf(null);
          const slot = i === -1 ? state.panes.length : i;
          state.panes[slot] = pane.session.id;
          saveLayout(state);
        }
      }).catch((e) => {
        console.error('multi-chat: could not open pane', e);
      });
      return pane;
    }

    function paneSlot(wide, open) {
      const slot = document.createElement('div');
      slot.className = (wide ? 'multi-chat-pane wide' : 'multi-chat-pane') + ' multi-chat-pane-slot';
      const head = document.createElement('div');
      head.className = 'multi-chat-pane-head';
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'secondary multi-chat-close';
      close.textContent = '×';
      close.title = 'Close this pane (the chat stays open on the server)';
      close.onclick = () => closeSlot(open.id);
      head.appendChild(close);
      const body = document.createElement('div');
      body.className = 'multi-chat-pane-body';
      body.append(open.nodes);
      slot.appendChild(head);
      slot.appendChild(body);
      body.addEventListener('focusin', () => { activeId = open.id; });
      return slot;
    }

    function placeholder(label, onClick, wide) {
      const el = document.createElement('div');
      el.className = (wide ? 'multi-chat-placeholder wide' : 'multi-chat-placeholder');
      el.style.cursor = 'pointer';
      el.textContent = label;
      el.onclick = onClick;
      return el;
    }

    // --- Dockview path; returns false to let the caller fall back ----------
    function tryRenderDockview(open) {
      if (typeof window.DockviewGrid !== 'function') return false;
      const hostEl = document.createElement('div');
      hostEl.style.width = '100%';
      hostEl.style.height = '100%';
      host.appendChild(hostEl);
      let grid = null;
      try {
        grid = new window.DockviewGrid(hostEl, {});
      } catch (e) {
        return false;
      }
      const n = open.length;
      open.forEach((o, i) => {
        let position = { direction: 'first' };
        if (n > 1) {
          if (state.preset === '2col') position = { direction: i === 0 ? 'first' : 'nextTo' };
          else if (state.preset === '2x2') {
            position = i === 0 ? { direction: 'first' }
              : i === 1 ? { direction: 'nextTo' }
              : i === 2 ? { direction: 'below' }
              : { direction: 'nextTo' };
          }
        }
        try {
          grid.addPane({
            position,
            component: {
              id: o.id, // keyed by session id so close events can map back
              render(el) {
                el.style.overflow = 'auto';
                mountPane(o.id, el, false);
                el.addEventListener('focusin', () => { activeId = o.id; });
              },
            },
          });
        } catch (e) {
          /* one slot failed: the rest already rendered, which is acceptable */
        }
      });
      // Dockview panes get their own close button; closing a pane there must be
      // DOM unmount + layout state only, never POST api/chat/<id>/close.
      if (grid && typeof grid.onClose === 'function' && open.length) {
        grid.onClose((pane) => {
          const id = pane && pane.component && pane.component.id;
          if (typeof id !== 'string' || !state.panes.includes(id)) return;
          const result = closePane(state, id);
          state = result.state;
          activeId = result.nextFocus || firstOpenPane(state);
          saveLayout(state);
          render();
        });
      }
      return true;
    }

    function isMobile() {
      return (window.innerWidth || 1024) < MOBILE_MAX_WIDTH;
    }

    function render() {
      host.innerHTML = ''; // unmounts every previous pane's DOM -- no network
      markPreset();
      const wide = state.preset === '1';
      const mounted = mountedSet(state, activeId);

      // Open panes in recency order, each with a live ChatPane when it is in the
      // mount pool (active + two most recent); otherwise a "open" placeholder.
      const open = state.panes.map((id) => {
        if (id === null) return null;
        if (!mounted.has(id)) {
          return { id, placeholder: true, nodes: null };
        }
        return { id, placeholder: false, nodes: null };
      }).filter(Boolean);

      if (isMobile()) {
        // Narrow viewport: one group with a switcher, per the design rule.
        const bar = document.createElement('div');
        bar.className = 'multi-chat-mobile-bar';
        const select = document.createElement('select');
        bar.appendChild(select);
        for (const o of open) {
          const opt = document.createElement('option');
          opt.value = o.id;
          opt.textContent = o.id;
          if (o.id === activeId) opt.selected = true;
          select.appendChild(opt);
        }
        const newOpt = document.createElement('option');
        newOpt.value = '__new__';
        newOpt.textContent = '+ New chat';
        select.appendChild(newOpt);
        select.onchange = () => {
          if (select.value === '__new__') { openNewChat(); return; }
          activeId = select.value;
          render();
        };
        host.appendChild(bar);
        const target = open.find((o) => o.id === activeId) || open[0];
        if (!target) {
          host.appendChild(placeholder('+ New chat', openNewChat, true));
          return;
        }
        const body = document.createElement('div');
        body.className = 'multi-chat-pane-body';
        mountPane(target.id, body, false);
        host.appendChild(paneSlot(true, { id: target.id, nodes: body }));
        return;
      }

      // Desktop: Dockview when available, otherwise a flexbox split.
      const mountedOpen = open.filter((o) => !o.placeholder);
      if (tryRenderDockview(mountedOpen) && mountedOpen.length === open.length) return;
      if (host.firstChild) host.removeChild(host.firstChild); // remove the abandoned dockview container

      // One ChatPane per pane (the spec for the fallback split): mount every open
      // slot, whatever the mount-pool decision was.
      const allOpen = open.map((o) => {
        const body = document.createElement('div');
        body.className = 'multi-chat-pane-body';
        mountPane(o.id, body, false);
        return { id: o.id, nodes: body };
      });

      const flex = document.createElement('div');
      flex.className = 'multi-chat-flex';
      let filled = 0;
      for (const id of state.panes) {
        if (id === null) {
          flex.appendChild(placeholder('+ New chat', openNewChat, wide));
          continue;
        }
        const o = allOpen.find((x) => x.id === id);
        if (o) {
          flex.appendChild(paneSlot(wide, o));
          filled++;
        } else {
          flex.appendChild(placeholder('Open chat…', () => { activeId = id; render(); }, wide));
        }
      }
      if (filled === 0) flex.appendChild(placeholder('+ New chat', openNewChat, wide));
      host.appendChild(flex);
    }

    function closeSlot(id) {
      // DOM unmount happens in render(); the session itself is NOT closed on the
      // server (no POST api/chat/<id>/close) -- it can be reopened from the list.
      const result = closePane(state, id);
      state = result.state;
      activeId = result.nextFocus || firstOpenPane(state);
      saveLayout(state);
      render();
    }

    function openNewChat() {
      // Claim a null slot (most recent first); append one when every slot is
      // filled (2x2). The pane is created first; its fresh id is recorded in the
      // layout state once startNew() has resolved (see mountPane).
      host.innerHTML = '';
      markPreset();
      const idx = state.panes.indexOf(null);
      if (idx === -1) state.panes.push(null);
      const wide = state.preset === '1';
      const flex = document.createElement('div');
      flex.className = 'multi-chat-flex';
      state.panes.forEach((id, i) => {
        if (i === (idx === -1 ? state.panes.length - 1 : idx)) {
          const body = document.createElement('div');
          body.className = 'multi-chat-pane-body';
          mountPane(null, body, true);
          flex.appendChild(paneSlot(wide, { id: '__new__', nodes: body }));
        } else if (id === null) {
          flex.appendChild(placeholder('+ New chat', openNewChat, wide));
        } else {
          flex.appendChild(placeholder('Open chat…', () => { activeId = id; render(); }, wide));
        }
      });
      host.appendChild(flex);
    }

    // Re-render only when crossing the 760px boundary (a plain resize must not
    // throw away in-flight pane state).
    let lastMobile = isMobile();
    window.addEventListener('resize', () => {
      const mobile = isMobile();
      if (mobile !== lastMobile) {
        lastMobile = mobile;
        render();
      }
    });

    import('./chat.js')
      .then((mod) => {
        ChatPane = mod.ChatPane;
        if (!ChatPane) throw new Error("multi-chat: './chat.js' does not export ChatPane");
        render();
      })
      .catch((e) => {
        console.error('multi-chat: failed to start multi-pane view', e);
        host.textContent = 'Multi-pane view failed to start: ' + (e && e.message ? e.message : e);
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}
