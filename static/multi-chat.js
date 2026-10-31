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

// Horizontal pane strip (owner request 2026-10-24: panes side by side in one row with previous/next buttons, swipe on touch). Pure helpers, no DOM.
// How many panes fit side by side: one below 760px (a phone), two up to 1399px, three from 1400px. A width that is not a number counts as a phone.
export function stripVisibleCount(width) {
  const w = Number(width);
  if (!(w >= 760)) return 1;
  return w < 1400 ? 2 : 3;
}

// The first visible pane's index, kept inside [0, max(0, paneCount - visibleCount)]; anything that is not a finite number gives 0.
function lastStripIndex(paneCount, visibleCount) {
  const count = Number.isFinite(Number(paneCount)) ? Math.max(0, Math.trunc(Number(paneCount))) : 0;
  const visible = Number.isFinite(Number(visibleCount)) ? Math.max(1, Math.trunc(Number(visibleCount))) : 1;
  return Math.max(0, count - visible);
}

export function clampStripIndex(index, paneCount, visibleCount) {
  const i = typeof index === 'number' && Number.isFinite(index) ? Math.trunc(index) : 0;
  return Math.min(lastStripIndex(paneCount, visibleCount), Math.max(0, i));
}

export function stepStrip(index, delta, paneCount, visibleCount) {
  const from = clampStripIndex(index, paneCount, visibleCount);
  const d = typeof delta === 'number' && Number.isFinite(delta) ? Math.trunc(delta) : 0;
  return clampStripIndex(from + d, paneCount, visibleCount);
}

// '2 / 4' for the first visible pane, or '1-2 / 4' when two or more panes are on screen; '' when there is no pane.
export function stripLabel(index, paneCount, visibleCount) {
  const count = Number.isFinite(Number(paneCount)) ? Math.max(0, Math.trunc(Number(paneCount))) : 0;
  if (count === 0) return '';
  const first = clampStripIndex(index, count, visibleCount) + 1;
  const last = Math.min(count, first - 1 + Math.max(1, Math.trunc(Number(visibleCount)) || 1));
  return last > first ? `${first}-${last} / ${count}` : `${first} / ${count}`;
}

export function stripButtons(index, paneCount, visibleCount) {
  const i = clampStripIndex(index, paneCount, visibleCount);
  return { canPrev: i > 0, canNext: i < lastStripIndex(paneCount, visibleCount) };
}

// Where a chat picked in the toolbar switcher goes. Already open: only the active chat changes. Otherwise the first empty slot, else it replaces the active pane
// (the last pane when `activeId` is null or not open), else it becomes the only pane. Returns { state, activeId }; the input is never mutated.
export function placeChatInStrip(state, id, activeId) {
  const panes = state && Array.isArray(state.panes) ? state.panes.slice() : [];
  const preset = state && typeof state.preset === 'string' ? state.preset : '1';
  if (typeof id !== 'string' || id.length === 0) return { state: { preset, panes }, activeId: activeId == null ? null : activeId };
  if (!panes.includes(id)) {
    const empty = panes.indexOf(null);
    const active = typeof activeId === 'string' ? panes.indexOf(activeId) : -1;
    if (empty !== -1) panes[empty] = id;
    else if (!panes.length) panes.push(id);
    else panes[active !== -1 ? active : panes.length - 1] = id;
  }
  return { state: { preset, panes }, activeId: id };
}

// Which pane is first on screen after the user scrolls or swipes the strip: the first pane whose left edge is at or past the scroll position (4px of slack for
// sub-pixel snapping), clamped to the last pane; 0 when there are no panes. `paneLefts` are the panes' left offsets inside the strip, in order.
export function indexFromScroll(scrollLeft, paneLefts) {
  const lefts = Array.isArray(paneLefts) ? paneLefts : [];
  if (!lefts.length) return 0;
  const x = typeof scrollLeft === 'number' && Number.isFinite(scrollLeft) ? scrollLeft : 0;
  const i = lefts.findIndex((l) => typeof l === 'number' && Number.isFinite(l) && l >= x - 4);
  return i === -1 ? lefts.length - 1 : i;
}

// The arrow keys move the strip only when the user is not typing: false inside an input, a textarea, a select or a contenteditable element.
export function shouldHandleArrowKey(tagName, isContentEditable = false) {
  if (isContentEditable) return false;
  const tag = String(tagName == null ? '' : tagName).toUpperCase();
  return !(tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT');
}

// The chat buttons at the top of the bar: one per open pane, in pane order. `titles` maps a session id to its title; a chat whose title is not known (yet) is called 'Chat'.
// Returns [{ id, title, active }]; `active` is true only for the pane that is `activeId`. Nothing is mutated.
export function chatTabs(state, titles, activeId) {
  const known = titles && typeof titles === 'object' ? titles : {};
  return openIds(state).map((id) => {
    const t = Object.prototype.hasOwnProperty.call(known, id) && typeof known[id] === 'string' ? known[id].trim() : '';
    return { id, title: t || 'Chat', active: id === activeId };
  });
}

// Layout templates (HUB0010 6/10): named {preset, count} recipes. Plain data in, plain data
// out; nothing here touches storage or the DOM, and no input is ever mutated.
function slotsFor(preset) {
  return preset === '2col' ? 2 : preset === '2x2' ? 4 : 1;
}

function idsOf(list) {
  return Array.isArray(list) ? list.filter((p) => typeof p === 'string' && p.length > 0) : [];
}

function copyTemplates(templates) {
  const out = {};
  for (const name of Object.keys(templates)) out[name] = { preset: templates[name].preset, count: templates[name].count };
  return out;
}

export const DEFAULT_TEMPLATES = {
  'driver + 2 workers': { preset: '2x2', count: 3 },
  'monitor grid': { preset: '2x2', count: 4 },
};

function ownTemplate(templates, name) {
  if (!templates || typeof templates !== 'object' || !Object.prototype.hasOwnProperty.call(templates, name)) return null;
  const t = templates[name];
  return t && PRESETS.includes(t.preset) ? t : null;
}

// A NEW templates object with `name` set to the preset and pane count of `state`.
export function saveTemplate(templates, name, state) {
  const out = copyTemplates(templates && typeof templates === 'object' ? templates : {});
  out[name] = { preset: state.preset, count: Array.isArray(state.panes) ? state.panes.length : 0 };
  return out;
}

// A NEW state with the template's preset: panes come from state.panes first, then from
// `openIdList` (ids not already used), up to the template's count (capped at the preset's
// slot count), and null placeholders pad it to the slot count. An unknown name (or a
// template with a bad preset) returns `state` unchanged.
export function applyTemplate(templates, name, state, openIdList) {
  const t = ownTemplate(templates, name);
  if (!t) return state;
  const slots = slotsFor(t.preset);
  const want = Math.min(Number.isInteger(t.count) && t.count > 0 ? t.count : 0, slots);
  const panes = [];
  for (const id of idsOf(state && state.panes).concat(idsOf(openIdList))) {
    if (panes.length >= want) break;
    if (!panes.includes(id)) panes.push(id);
  }
  while (panes.length < slots) panes.push(null);
  return { preset: t.preset, panes };
}

export function serializeTemplates(templates) {
  return JSON.stringify(templates && typeof templates === 'object' ? templates : {});
}

// Malformed JSON, or anything that is not a plain object, gives a copy of DEFAULT_TEMPLATES.
// Entries with an unknown preset, or a count that is not a whole number >= 0, are dropped.
export function deserializeTemplates(json) {
  let parsed = null;
  try {
    parsed = JSON.parse(json);
  } catch (e) {
    return copyTemplates(DEFAULT_TEMPLATES);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return copyTemplates(DEFAULT_TEMPLATES);
  const out = {};
  for (const name of Object.keys(parsed)) {
    const t = parsed[name];
    if (t && PRESETS.includes(t.preset) && Number.isInteger(t.count) && t.count >= 0) {
      out[name] = { preset: t.preset, count: t.count };
    }
  }
  return out;
}

// ============================================================================
// Browser-only adapter. Node imports of this module stop at the guard above.
// ============================================================================
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const STORAGE_KEY = 'chat-workspace-layout';
  const TEMPLATES_KEY = 'chat-workspace-templates';
  const TEMPLATE_NAME_MAX = 40;
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

  // Saved templates (HUB0010 7/10): the built-ins plus whatever the user saved in this
  // browser. Every storage access is guarded; without storage the templates just last
  // until reload.
  function loadTemplates() {
    const merged = copyTemplates(DEFAULT_TEMPLATES);
    let raw = null;
    try {
      raw = window.localStorage.getItem(TEMPLATES_KEY);
    } catch (e) {
      raw = null;
    }
    if (raw != null) Object.assign(merged, deserializeTemplates(raw));
    return merged;
  }

  function persistTemplates(templates) {
    try {
      window.localStorage.setItem(TEMPLATES_KEY, serializeTemplates(templates));
    } catch (e) { /* storage disabled/unavailable: the template just won't persist */ }
  }

  function injectStyles() {
    if (document.getElementById('multi-chat-style')) return;
    const style = document.createElement('style');
    style.id = 'multi-chat-style';
    style.textContent =
      '.multi-chat-toolbar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:8px 12px; border-bottom:1px solid var(--border,#ddd); }' +
      // The strip layout (`.multi-chat-flex` / `.multi-chat-pane` / `.wide` sizing, scroll-snap)
      // lives in static/chat.css: one horizontal row, --strip-visible panes per screen.
      '.multi-chat-host { flex:1; min-height:0; overflow:hidden; position:relative; display:flex; flex-direction:column; }' +
      '.multi-chat-pane-body { flex:1; min-height:0; overflow-y:auto; }' +
      '.multi-chat-placeholder { border:2px dashed var(--border,#ccc); border-radius:8px; align-items:center; justify-content:center; background:transparent; }';
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

    // --- toolbar: the open chats as a row of buttons (one per pane, the active one highlighted) and a "+" new-chat button ----------------
    const tabsBox = document.createElement('div');
    tabsBox.className = 'multi-chat-tabs';
    tabsBox.setAttribute('role', 'tablist');
    tabsBox.setAttribute('aria-label', 'Open chats');
    toolbar.appendChild(tabsBox);
    const newTabBtn = document.createElement('button');
    newTabBtn.type = 'button';
    newTabBtn.className = 'secondary multi-chat-new-btn';
    newTabBtn.textContent = '+';
    newTabBtn.title = 'New chat';
    newTabBtn.setAttribute('aria-label', 'New chat');
    newTabBtn.onclick = () => openNewChat();
    toolbar.appendChild(newTabBtn);

    // --- toolbar: the session switcher, now the "All chats" list (built once chat.js has loaded) -----
    const switcherSlot = document.createElement('div');
    switcherSlot.className = 'multi-chat-switcher';
    toolbar.appendChild(switcherSlot);
    let switcher = null;
    const refreshSwitcher = () => { if (switcher) switcher.refresh().then(renderTabs, () => {}); renderTabs(); };
    function renderTabs() {
      const titles = {};
      if (switcher && Array.isArray(switcher.rows)) for (const r of switcher.rows) if (r && typeof r.id === 'string') titles[r.id] = r.title;
      for (const p of livePanes) if (p.sessionId && typeof titles[p.sessionId] === 'string') p.setTitle(titles[p.sessionId]); // pane headers carry the same real title as the buttons
      tabsBox.innerHTML = '';
      for (const tab of chatTabs(state, titles, activeId)) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'secondary multi-chat-tab';
        btn.textContent = tab.title;
        btn.title = tab.title;
        btn.setAttribute('role', 'tab');
        btn.setAttribute('aria-selected', String(tab.active));
        if (tab.active) btn.setAttribute('aria-current', 'true');
        btn.onclick = () => selectChat(tab.id);
        tabsBox.appendChild(btn);
      }
    }

    // --- toolbar: one "Layout" control at the end of the bar; the presets and the templates live in its popover ----------------
    const layoutBox = document.createElement('div');
    layoutBox.className = 'multi-chat-layout';
    const layoutBtn = document.createElement('button');
    layoutBtn.type = 'button';
    layoutBtn.className = 'secondary multi-chat-layout-btn';
    layoutBtn.textContent = 'Layout \u25BE';
    layoutBtn.setAttribute('aria-haspopup', 'true');
    layoutBtn.setAttribute('aria-expanded', 'false');
    const layoutMenu = document.createElement('div');
    layoutMenu.className = 'multi-chat-layout-menu';
    layoutMenu.hidden = true;
    const presetRow = document.createElement('div');
    presetRow.className = 'multi-chat-presets';
    layoutMenu.appendChild(presetRow);
    layoutBox.appendChild(layoutBtn);
    layoutBox.appendChild(layoutMenu);
    function setLayoutOpen(open) {
      layoutMenu.hidden = !open;
      layoutBtn.setAttribute('aria-expanded', String(open));
    }
    layoutBtn.onclick = (e) => { e.stopPropagation(); setLayoutOpen(layoutMenu.hidden); };
    document.addEventListener('click', (e) => { if (!layoutMenu.hidden && !layoutBox.contains(e.target)) setLayoutOpen(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !layoutMenu.hidden) setLayoutOpen(false); });

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
        setLayoutOpen(false);
        render();
      };
      presetRow.appendChild(btn);
      presetButtons[preset] = btn;
    }
    function markPreset() {
      for (const p of PRESETS) presetButtons[p].setAttribute('aria-pressed', String(state.preset === p));
    }

    // --- toolbar: templates menu + inline "save current as" (no window.prompt) ---
    let templates = loadTemplates();
    const tplBox = document.createElement('div');
    tplBox.className = 'multi-chat-templates';
    const tplSelect = document.createElement('select');
    tplSelect.className = 'multi-chat-template-select';
    tplSelect.setAttribute('aria-label', 'Layout templates');
    const tplSaveBtn = document.createElement('button');
    tplSaveBtn.type = 'button';
    tplSaveBtn.className = 'secondary multi-chat-template-save-btn';
    tplSaveBtn.textContent = 'Save current as…';
    const tplSaveRow = document.createElement('span');
    tplSaveRow.className = 'multi-chat-template-save';
    tplSaveRow.hidden = true;
    const tplName = document.createElement('input');
    tplName.type = 'text';
    tplName.className = 'multi-chat-template-name';
    tplName.maxLength = TEMPLATE_NAME_MAX;
    tplName.placeholder = 'Template name';
    tplName.setAttribute('aria-label', 'Template name');
    const tplOk = document.createElement('button');
    tplOk.type = 'button';
    tplOk.className = 'multi-chat-template-ok';
    tplOk.textContent = 'Save';
    tplSaveRow.appendChild(tplName);
    tplSaveRow.appendChild(tplOk);
    tplBox.appendChild(tplSelect);
    tplBox.appendChild(tplSaveBtn);
    tplBox.appendChild(tplSaveRow);
    layoutMenu.appendChild(tplBox);

    // --- toolbar: previous / next buttons and the position label for the pane strip ----------------
    // The strip (.multi-chat-flex, one snap row) scrolls natively by touch swipe or mouse; these buttons, the label and the arrow keys are the other ways to move.
    const nav = document.createElement('div');
    nav.className = 'multi-chat-strip-nav';
    nav.hidden = true;
    const prevBtn = document.createElement('button');
    prevBtn.type = 'button';
    prevBtn.className = 'secondary multi-chat-strip-btn';
    prevBtn.textContent = '\u25C0';
    prevBtn.title = 'Previous chat';
    prevBtn.setAttribute('aria-label', 'Previous chat');
    const stripLabelEl = document.createElement('span');
    stripLabelEl.className = 'multi-chat-strip-label';
    stripLabelEl.setAttribute('aria-live', 'polite');
    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.className = 'secondary multi-chat-strip-btn';
    nextBtn.textContent = '\u25B6';
    nextBtn.title = 'Next chat';
    nextBtn.setAttribute('aria-label', 'Next chat');
    nav.appendChild(prevBtn);
    nav.appendChild(stripLabelEl);
    nav.appendChild(nextBtn);
    toolbar.appendChild(nav);
    toolbar.appendChild(layoutBox); // last: the Layout control closes the bar

    let strip = null; // the current .multi-chat-flex element, or null (dockview, or nothing drawn)
    let dockGrid = null; // the live window.DockviewGrid, or null; disposed before every redraw so an old grid stops listening
    function disposeDockGrid() {
      if (!dockGrid) return;
      try { if (typeof dockGrid.dispose === 'function') dockGrid.dispose(); } catch (e) { /* the host was already cleared */ }
      dockGrid = null;
    }
    let stripIndex = 0;
    let stripScrollTimer = null;
    const stripPanes = () => (strip ? Array.from(strip.children) : []);
    const stripLefts = () => stripPanes().map((p) => p.offsetLeft - strip.offsetLeft);
    function updateStripNav() {
      const count = stripPanes().length;
      nav.hidden = !strip || count < 2;
      if (nav.hidden) return;
      const visible = stripVisibleCount(window.innerWidth || 1024);
      stripIndex = clampStripIndex(stripIndex, count, visible);
      stripLabelEl.textContent = stripLabel(stripIndex, count, visible);
      const b = stripButtons(stripIndex, count, visible);
      prevBtn.disabled = !b.canPrev;
      nextBtn.disabled = !b.canNext;
    }
    function scrollStripTo(i, smooth) {
      const pane = stripPanes()[i];
      if (!strip || !pane) return;
      strip.scrollTo({ left: pane.offsetLeft - strip.offsetLeft, behavior: smooth ? 'smooth' : 'auto' });
    }
    function stepStripBy(delta) {
      const count = stripPanes().length;
      const next = stepStrip(stripIndex, delta, count, stripVisibleCount(window.innerWidth || 1024));
      if (!strip || next === stripIndex) return false;
      stripIndex = next;
      scrollStripTo(next, true);
      const landed = state.panes[next];
      if (typeof landed === 'string') activeId = landed; // the highlighted chat follows the strip
      renderTabs();
      updateStripNav();
      return true;
    }
    // Called with each freshly built strip: keep the position across a re-render, follow the user's own scrolling and swiping (debounced 80 ms).
    function attachStrip(flex) {
      strip = flex;
      scrollStripTo(clampStripIndex(stripIndex, stripPanes().length, stripVisibleCount(window.innerWidth || 1024)), false);
      updateStripNav();
      flex.addEventListener('scroll', () => {
        if (stripScrollTimer !== null) clearTimeout(stripScrollTimer);
        stripScrollTimer = setTimeout(() => {
          stripScrollTimer = null;
          if (strip !== flex) return;
          stripIndex = clampStripIndex(indexFromScroll(flex.scrollLeft, stripLefts()), stripPanes().length, stripVisibleCount(window.innerWidth || 1024));
          updateStripNav();
        }, 80);
      });
    }
    prevBtn.onclick = () => stepStripBy(-1);
    nextBtn.onclick = () => stepStripBy(1);
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target;
      if (!shouldHandleArrowKey(t && t.tagName, !!(t && t.isContentEditable))) return;
      if (stepStripBy(e.key === 'ArrowRight' ? 1 : -1)) e.preventDefault();
    });

    function refreshTemplateMenu() {
      tplSelect.innerHTML = '';
      const head = document.createElement('option');
      head.value = '';
      head.textContent = 'Templates…';
      tplSelect.appendChild(head);
      for (const name of Object.keys(templates)) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        tplSelect.appendChild(opt);
      }
      tplSelect.value = '';
    }
    refreshTemplateMenu();

    tplSelect.onchange = () => {
      const name = tplSelect.value;
      tplSelect.value = '';
      if (!name) return;
      const openList = state.panes.filter((p) => typeof p === 'string' && p.length > 0);
      state = applyTemplate(templates, name, state, openList);
      activeId = firstOpenPane(state) || activeId;
      saveLayout(state);
      setLayoutOpen(false);
      render();
    };
    function closeTemplateSave() {
      tplSaveRow.hidden = true;
      tplName.value = '';
    }
    tplSaveBtn.onclick = () => {
      tplSaveRow.hidden = !tplSaveRow.hidden;
      if (!tplSaveRow.hidden) tplName.focus();
    };
    function commitTemplate() {
      const name = tplName.value.trim().slice(0, TEMPLATE_NAME_MAX);
      if (!name || name === '__proto__') return; // empty names and the one name that would rewrite the object's prototype
      templates = saveTemplate(templates, name, state);
      persistTemplates(templates);
      refreshTemplateMenu();
      closeTemplateSave();
    }
    tplOk.onclick = commitTemplate;
    tplName.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commitTemplate(); }
      else if (e.key === 'Escape') closeTemplateSave();
    });

    // --- one ChatPane inside a framed slot (close = DOM unmount, no API) ----
    let ChatPane = null;
    let livePanes = []; // the ChatPane instances drawn by the current render, for the dashboard's refresh message
    let pendingNewAt = -1; // the slot index the next render() fills with a brand-new chat (set by openNewChat), else -1
    let showToast = (msg) => console.error(msg);
    function mountPane(id, bodyEl, isNew) {
      const pane = new ChatPane(bodyEl, { onSessionChange: () => refreshSwitcher() }); // a new title or a busy flag shows up on the chat's button
      livePanes.push(pane);
      const opener = isNew ? pane.startNew() : pane.loadSession(id);
      // After startNew the pane knows its fresh session id -- record it so the
      // saved layout stores a real id instead of a placeholder.
      opener.then(() => {
        if (isNew && pane.session && pane.session.id) {
          const i = state.panes.indexOf(null);
          const slot = i === -1 ? state.panes.length : i;
          state.panes[slot] = pane.session.id;
          saveLayout(state);
          activeId = pane.session.id; // the new chat's button is the highlighted one
          renderTabs();
          refreshSwitcher();
        }
      }).catch((e) => {
        console.error('multi-chat: could not open pane', e);
      });
      return pane;
    }

    function paneSlot(wide, open) {
      const slot = document.createElement('div');
      slot.className = (wide ? 'multi-chat-pane wide' : 'multi-chat-pane') + ' multi-chat-pane-slot';
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'secondary multi-chat-close';
      close.textContent = '×';
      close.title = 'Close this pane (the chat stays open on the server)';
      close.onclick = () => closeSlot(open.id);
      const body = document.createElement('div');
      body.className = 'multi-chat-pane-body';
      body.append(open.nodes);
      // One header line per pane: the close button is the last button of the pane's own .chat-header (title left, buttons right).
      const header = body.querySelector('.chat-header');
      if (header) header.appendChild(close);
      else body.prepend(close);
      slot.appendChild(body);
      body.addEventListener('focusin', () => { activeId = open.id; renderTabs(); });
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
      dockGrid = grid;
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
      disposeDockGrid();
      host.innerHTML = ''; // unmounts every previous pane's DOM -- no network
      livePanes = [];
      renderTabs();
      Promise.resolve().then(refreshSwitcher); // after this render has finished, whichever way it returns
      strip = null;
      updateStripNav();
      markPreset();
      // Owner 2026-10-24: the strip always shows the saved layout at every width.
      // Below 760px the CSS sets --strip-visible to 1 (static/chat.css), so a phone
      // shows one whole pane per screen and a touch swipe moves to the next.
      // render never collapses `state` (and never saves).
      const view = state;
      const wide = view.preset === '1';
      const mounted = mountedSet(view, activeId);

      // Open panes in recency order, each with a live ChatPane when it is in the
      // mount pool (active + two most recent); otherwise a "open" placeholder.
      const open = view.panes.map((id) => {
        if (id === null) return null;
        if (!mounted.has(id)) {
          return { id, placeholder: true, nodes: null };
        }
        return { id, placeholder: false, nodes: null };
      }).filter(Boolean);

      // One row of panes: Dockview when available, otherwise the flexbox strip.
      const mountedOpen = open.filter((o) => !o.placeholder);
      if (tryRenderDockview(mountedOpen) && mountedOpen.length === open.length) return;
      disposeDockGrid();
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
      for (let i = 0; i < state.panes.length; i++) {
        const id = state.panes[i];
        if (id === null && i === pendingNewAt) {
          pendingNewAt = -1;
          const body = document.createElement('div');
          body.className = 'multi-chat-pane-body';
          mountPane(null, body, true);
          flex.appendChild(paneSlot(wide, { id: '__new__', nodes: body }));
          filled++;
          continue;
        }
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
      attachStrip(flex);
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
      // Claim a null slot (most recent first); append one when every slot is filled (2x2). render() mounts a fresh chat in that slot
      // next to every chat that is already open; its new id is recorded in the layout state once startNew() has resolved (see mountPane).
      const idx = state.panes.indexOf(null);
      if (idx === -1) state.panes.push(null);
      pendingNewAt = idx === -1 ? state.panes.length - 1 : idx;
      stripIndex = pendingNewAt; // the strip scrolls to the new chat (clamped to the last position that fits)
      render();
    }

    // Re-render only when crossing the 760px boundary (a plain resize must not
    // throw away in-flight pane state); debounced 150 ms so a drag settles first.
    let lastMobile = isMobile();
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      if (resizeTimer !== null) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeTimer = null;
        const mobile = isMobile();
        if (mobile !== lastMobile) {
          lastMobile = mobile;
          render();
        } else {
          updateStripNav();
        }
      }, 150);
    });

    function selectChat(id) {
      const wasOpen = state.panes.includes(id);
      const placed = placeChatInStrip(state, id, activeId);
      state = placed.state;
      activeId = placed.activeId;
      const at = state.panes.indexOf(id);
      if (at !== -1) stripIndex = at;
      if (wasOpen && strip) {
        scrollStripTo(clampStripIndex(stripIndex, stripPanes().length, stripVisibleCount(window.innerWidth || 1024)), true);
        updateStripNav();
        renderTabs();
        refreshSwitcher();
        return;
      }
      saveLayout(state);
      render();
    }
    async function closeChat(id) {
      try {
        const resp = await fetch(`api/chat/${encodeURIComponent(id)}/close`, { method: 'POST' });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.description || `HTTP ${resp.status}`);
        }
      } catch (e) {
        showToast('Could not close that chat: ' + e.message);
        return;
      }
      const result = closePane(state, id);
      state = result.state;
      if (activeId === id) activeId = result.nextFocus || firstOpenPane(state);
      saveLayout(state);
      render();
    }

    // The dashboard's sendTextToChat() posts to api/chat/inject, then postMessages this iframe: re-fetch every pane that is not mid-reply.
    window.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'agent-manager-chat-refresh') {
        for (const p of livePanes) if (!p.isSending) p.load();
      }
    });

    // The page is never empty: with no chat open, show the most recent one, or start a new chat when there are none.
    async function ensureOnePane() {
      if (openIds(state).length > 0) return;
      let rows = null;
      try {
        const resp = await fetch('api/chat/sessions');
        if (resp.ok) rows = await resp.json();
      } catch (e) { /* the list could not be read */ }
      if (!Array.isArray(rows)) return; // keep the '+ New chat' placeholder rather than start a stray chat on a failed read
      const latest = rows.length && typeof rows[0].id === 'string' ? rows[0].id : null;
      if (latest) selectChat(latest);
      else openNewChat();
    }

    import('./chat.js')
      .then((mod) => {
        ChatPane = mod.ChatPane;
        if (!ChatPane) throw new Error("multi-chat: './chat.js' does not export ChatPane");
        showToast = mod.showToast;
        switcher = new mod.SessionSwitcher(switcherSlot, {
          getSelectedId: () => activeId,
          getRoots: () => [],
          onSelect: selectChat,
          onNew: openNewChat,
          onClose: closeChat,
          buttonLabel: 'All chats',
        });
        render();
        ensureOnePane();
      })
      .catch((e) => {
        console.error('multi-chat: failed to start multi-pane view', e);
        host.textContent = 'Multi-pane view failed to start: ' + (e && e.message ? e.message : e);
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}
