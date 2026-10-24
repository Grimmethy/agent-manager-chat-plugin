// Standalone chat.js for this repo's own chat.html, served through the host dashboard's
// generic proxy (see chat.html's own comment on why every URL here is relative, never
// absolute). Ported from agent-manager's static/js/core-ui.js chat*-prefixed functions --
// see git history there for the pre-extraction version.
//
// Deliberately independent of the host's shared grillRenderTranscript()/.grill-* CSS
// (Discuss/Grill Me panels) -- see this repo's chat.html header comment and
// /home/wok/.claude/plans/immutable-noodling-axolotl.md's "Shared frontend rendering"
// section. chatSetCollapsed()/#chat-toggle-tab are also NOT ported: this page fills its
// whole iframe document now, so show/hide is the HOST's job (toggling the iframe's own
// container), not something this page controls from inside.
//
// Simplification vs. the in-tree version: the "long words 10% brighter" prose styling
// (core-ui.js's escapeHtmlBright/markLongWords) is not ported -- purely cosmetic, and
// porting it would mean duplicating a whole marker-based escaping scheme for one visual
// flourish. Plain escapeHtml() here.
//
// Assistant markdown rendering (2026-09-22, Grimmethy: "check github for something that
// handles this issue well" re: unreadable/unformatted assistant responses): via vendored
// static/vendor/smd.js (thetarnav/streaming-markdown, MIT -- see that file's own header
// and static/vendor/README.md for why this one). Only assistant text is rendered as
// markdown; user messages stay plain-escaped text -- a user's own typed message isn't
// markdown they're asking to be formatted, and rendering it would just be a bigger HTML
// surface for no benefit.
import * as smd from "./vendor/smd.js";
import { truncateTitle, orderSessions, nextSelectionAfterClose, formatRow,
  formatTerminalRow, orderTerminalSessions, applyTerminalEvent, hostsOf, filterByHost, skippedLabel } from "./sessions-view.js";
// HUB0008 10/11: the model-swap UI's WHAT-to-show logic lives in swap-view.js
// (piece 9) -- this file only does the DOM + fetches, mirroring the sessions-view
// split. providerLabel drives the header label, swapOptions builds the Swap menu,
// swapBody builds each POST body, offerText renders the failed-turn one-click offer.
import { providerLabel, swapOptions, offerText, swapBody } from "./swap-view.js";

let toastHideTimer = null;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function fetchJson(url, { timeoutMs = 8000 } = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(url, { signal: controller.signal });
  } catch (e) {
    if (e.name === 'AbortError') throw new Error(url + ' -> timed out after ' + (timeoutMs / 1000) + 's');
    throw e;
  } finally {
    clearTimeout(timeoutId);
  }
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}

function showToast(message, kind = 'error') {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = kind === 'error' ? 'toast-error' : 'toast-info';
  void el.offsetWidth; // force reflow so the fade-in replays on a reused node
  el.classList.add('show');
  clearTimeout(toastHideTimer);
  toastHideTimer = setTimeout(() => { el.classList.remove('show'); }, 3000);
}

// Renders one segment of assistant text as markdown into an HTML string, via a fresh
// smd Parser each call. Re-parsing the whole segment from scratch on every
// chatRender() (rather than keeping one Parser alive across streamed chunks, which is
// smd's more typical usage) matches how this file already rebuilds the ENTIRE
// transcript's innerHTML on every render -- see chatRender()'s own
// `transcriptEl.innerHTML = ...` -- so a persistent per-message Parser tied to DOM
// nodes that get thrown away each render would just be dead weight. Message-length
// text re-parses fast enough that this isn't a real cost.
function renderMarkdown(text) {
  const container = document.createElement('div');
  const parser = smd.parser(smd.default_renderer(container));
  smd.parser_write(parser, String(text));
  smd.parser_end(parser);
  return container.innerHTML;
}

// Own transcript renderer, deliberately NOT the host's shared grillRenderTranscript()
// (an iframe can't call a function in a different document anyway) -- see this file's
// own header comment.
function chatRenderTranscript(transcript, session) {
  const assistantLabel = session && session.provider === 'claude' ? 'Claude' : 'Local';
  const rows = transcript.map((t, idx) => {
    const roleLabel = t.role === 'assistant' ? assistantLabel : 'You';
    const cls = (t.role === 'assistant' ? 'chat-msg-assistant' : 'chat-msg-user') + (t.error ? ' chat-msg-error' : '');
    const isAssistant = t.role === 'assistant' && !t.error; // error notices stay literal text, not markdown (underscores in env var names get eaten)
    let body;
    if (t.timestamp) {
      const m = String(t.timestamp).match(/T(\d{2}):(\d{2}):(\d{2})/);
      const stamp = m ? `${m[1]}:${m[2]}:${m[3]}` : String(t.timestamp).slice(11, 19);
      // A <div> wrapper per segment, not <p> -- rendered markdown can itself start
      // with a block element (<pre>, <ul>, a heading, ...), and a block element
      // inside a <p> gets silently split by the HTML parser, breaking the timestamp
      // span's placement.
      body = String(t.text).split(/\n{2,}/).map(seg =>
        `<div class="chat-msg-seg"><span class="chat-seg-ts">${escapeHtml(stamp)}</span>${isAssistant ? renderMarkdown(seg) : escapeHtml(seg)}</div>`
      ).join('');
    } else {
      body = isAssistant ? renderMarkdown(t.text) : escapeHtml(t.text);
    }
    const cards = (t.templates || []).map((tp, ti) => {
      const done = tp.status === 'saved';
      const note = tp.message ? `<div class="tpl-note tpl-${escapeHtml(tp.status)}">${escapeHtml(tp.message)}</div>` : '';
      return `<div class="tpl-card" data-entry="${idx}" data-tpl="${ti}">
        <div class="tpl-head">Template proposal &middot; ${escapeHtml(tp.kind)}</div>
        <input class="tpl-key" value="${escapeHtml(tp.key)}" ${done ? 'disabled' : ''}>
        <textarea class="tpl-text" rows="6" ${done ? 'disabled' : ''}>${escapeHtml(tp.text)}</textarea>
        <button type="button" class="tpl-save" ${done ? 'disabled' : ''}>${done ? 'Saved' : 'Save template'}</button>${note}</div>`;
    }).join('');
    return `<div class="${cls}"><div class="chat-msg-role">${escapeHtml(roleLabel)}</div><div class="chat-msg-text">${body}</div>${cards}</div>`;
  }).join('');
  return `<div class="field-label">Conversation</div><div class="chat-transcript">${rows}</div>`;
}


function providerButtonLabel(provider) {
  return provider === 'claude' ? 'Claude' : 'Local';
}

// Menu / offer-button text for one swap option. swap-view.js's swapOptions entries
// carry a `label`; the failed-turn offer options the route builds (HUB0008 8/11,
// routes/chat.py) only carry {provider, account?}, so fall back to the account's
// label, or 'Local model' -- the exact label swap-view.js uses for its own local entry.
function swapOptionLabel(option) {
  const o = option || {};
  if (o.label) return o.label;
  if (o.provider === 'local') return 'Local model';
  return o.account || 'Claude';
}

// One chat: owns its session, its DOM (built inside `root`), its isSending guard and its
// provider toggle. Every DOM lookup and event listener is scoped to `root` (class
// selectors, no document-wide ids), so several panes can coexist on one page later.
// `onSessionChange(session)` fires whenever the pane's session is replaced or updated, so
// the page can keep the session switcher in step.
class ChatPane {
  constructor(root, { onSessionChange } = {}) {
    this.root = root;
    this.onSessionChange = onSessionChange || (() => {});
    this.session = null;
    // The selected chat id, kept as page state (not a cookie). Survives a failed fetch so
    // a refresh still knows which chat is "current".
    this.sessionId = null;
    this.providerChoice = 'local';
    // Guards BOTH ways a message can be submitted (Send-button click and Enter in the
    // input) against firing a second concurrent turn while one is still streaming. The
    // button's own `disabled` state used to be the only guard, but the Enter-key handler
    // never checked it -- so pressing Enter while a reply was still in flight fired a
    // second POST /api/chat/<id>/message for the same session, racing the first one on
    // the backend's session file (no locking there: two `stream_message()` calls each
    // hold their own in-memory snapshot of the whole transcript and rewrite the whole
    // file on every chunk, so whichever finishes last silently clobbers the other's
    // entries). Root-caused live 2026-09-25 from a session where a slow reply got
    // reprompted mid-stream and its real answer ended up buried under the next turn's.
    this.isSending = false;
    this.build();
  }

  build() {
    this.root.innerHTML = `
  <div class="chat-header">
    <div class="chat-switcher-slot"></div>
    <span class="chat-provider-container"></span>
    <span class="chat-provider-label" hidden></span>
    <span class="chat-swap-slot">
      <button type="button" class="secondary chat-swap-btn" hidden>Swap</button>
      <div class="chat-swap-menu" hidden></div>
    </span>
    <button type="button" class="secondary chat-reserve-btn" style="display:none" title="Hold the local model exclusively for this conversation until you turn it back off">Reserve</button>
    <button type="button" class="secondary chat-context-log-btn" title="View this session's context log -- a compacted, durable record built from CONTEXT-LOG: blocks at the end of each response, cross-referenced to any task discussed">Log</button>
  </div>
  <div class="chat-context-log-panel" hidden>
    <div class="chat-context-log-header">
      <span>Context Log</span>
      <button type="button" class="secondary chat-context-log-close">Close</button>
    </div>
    <div class="chat-context-log-content"></div>
  </div>
  <div class="chat-body">
    <div class="chat-transcript-host"></div>
    <textarea class="chat-input" rows="3" placeholder="Talk to the chat... (Enter to send, Shift+Enter for a new line)"></textarea>
    <div class="chat-send-row">
      <button type="button" class="action chat-send-btn">Send</button>
    </div>
  </div>`;
    const q = (sel) => this.root.querySelector(sel);
    this.switcherSlot = q('.chat-switcher-slot');
    this.providerContainer = q('.chat-provider-container');
    // HUB0008 10/11: header provider label + Swap menu (see syncSwapHeader below).
    this.providerLabelEl = q('.chat-provider-label');
    this.swapBtn = q('.chat-swap-btn');
    this.swapMenu = q('.chat-swap-menu');
    this.swapOpen = false;
    this.swapChoices = [];      // current menu/offer entries; data-swap-index points into this
    this.pendingSwapOffer = null; // failed-turn swapOffer (error event, piece 8), rendered by render()
    this.reserveBtn = q('.chat-reserve-btn');
    this.logBtn = q('.chat-context-log-btn');
    this.logPanel = q('.chat-context-log-panel');
    this.logContent = q('.chat-context-log-content');
    this.logClose = q('.chat-context-log-close');
    this.transcriptEl = q('.chat-transcript-host');
    this.input = q('.chat-input');
    this.sendBtn = q('.chat-send-btn');

    this.providerBtn = document.createElement('button');
    this.providerBtn.type = 'button';
    this.providerBtn.className = 'secondary provider-toggle';
    this.providerBtn.title = 'Local model vs. your Claude subscription -- click to switch';
    this.providerContainer.appendChild(this.providerBtn);
    this.syncProviderToggle();
    this.providerBtn.onclick = async (e) => {
      e.stopPropagation();
      if (this.isSending) {
        showToast('Wait for the current reply to finish before switching provider.', 'info');
        return;
      }
      this.providerChoice = this.providerChoice === 'local' ? 'claude' : 'local';
      this.syncProviderToggle();
      await this.startNew();
    };

    // HUB0008 10/11: Swap menu button; outside clicks and Escape close it (same
    // pattern as the session switcher's menu).
    this.swapBtn.onclick = (e) => { e.stopPropagation(); this.setSwapOpen(!this.swapOpen); };
    document.addEventListener('click', (e) => { if (this.swapOpen && !this.root.contains(e.target)) this.setSwapOpen(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.swapOpen) this.setSwapOpen(false); });

    this.sendBtn.onclick = () => this.send();
    this.input.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (this.isSending) return;
        this.send();
      }
    };
    this.reserveBtn.onclick = () => this.toggleReserve();
    this.logBtn.onclick = () => this.showContextLog();
    this.logClose.onclick = () => { this.logPanel.hidden = true; };

    // Save button on a template proposal card (event delegation: render() rebuilds the
    // whole transcript's innerHTML, so per-card listeners would be thrown away every
    // render). Scoped to this pane's root. Reads the card's CURRENT name/text so the
    // user's edits are what get saved; the server persists the outcome (saved / why not)
    // on the proposal, so it survives a reload.
    this.root.addEventListener('click', (e) => this.onRootClick(e));
  }

  setSession(session) {
    const prevId = this.sessionId;
    this.session = session;
    this.sessionId = session ? session.id : null;
    // A DIFFERENT chat was loaded: a pending swap offer or an open Swap menu belong to
    // the previous chat and must not leak into this one. (Same-id updates -- e.g. the
    // swap response itself -- keep state; performSwap() clears the offer explicitly.)
    if (prevId !== this.sessionId) {
      this.pendingSwapOffer = null;
      this.setSwapOpen(false);
    }
    this.onSessionChange(session);
  }

  syncProviderToggle() {
    const claude = this.providerChoice === 'claude';
    this.providerBtn.dataset.provider = this.providerChoice;
    this.providerBtn.textContent = providerButtonLabel(this.providerChoice);
    this.providerBtn.classList.toggle('provider-toggle-claude', claude);
  }

  // HUB0008 10/11: the header provider label (swap-view.providerLabel) and the Swap
  // button's visibility track the loaded session. All decisions -- what the label is,
  // which entries the menu lists, what each POST body looks like -- live in
  // static/swap-view.js (piece 9); this method only places them in the DOM. No token
  // or token path ever touches the client here: the menu entries and the swap body
  // carry only labels (swapBody), and the accounts endpoint returns public_view only.
  syncSwapHeader() {
    const has = !!this.session;
    this.providerLabelEl.textContent = has ? providerLabel(this.session) : '';
    this.providerLabelEl.hidden = !has;
    this.swapBtn.hidden = !has;
    if (!has) this.setSwapOpen(false);
  }

  setSwapOpen(open) {
    this.swapOpen = open;
    this.swapMenu.hidden = !open;
    if (open) this.renderSwapMenu();
  }

  // Builds the Swap menu from GET api/chat/accounts (public_view shape: {accounts:
  // [{label, host, live}]}) through swap-view.swapOptions, which drops the session's
  // pinned account and appends the local model (unless the session is already local).
  async renderSwapMenu() {
    const session = this.session;
    if (!session) return;
    this.swapMenu.innerHTML = '<div class="chat-switcher-empty">Loading accounts&hellip;</div>';
    let accounts;
    try {
      const body = await fetchJson('api/chat/accounts');
      accounts = Array.isArray(body.accounts) ? body.accounts : [];
    } catch (e) {
      this.swapMenu.innerHTML = `<div class="chat-switcher-empty">Could not load accounts: ${escapeHtml(e.message)}</div>`;
      return;
    }
    const options = swapOptions(accounts, session);
    this.swapChoices = options;
    this.swapMenu.innerHTML = options.length
      ? options.map((o, i) =>
        `<div class="chat-switcher-row" role="option" data-swap-index="${i}">
           <span class="chat-switcher-main"><span class="chat-switcher-title">${escapeHtml(swapOptionLabel(o))}</span></span>
         </div>`).join('')
      : '<div class="chat-switcher-empty">Nothing else to switch to.</div>';
  }

  // Performs the swap -- the ONE code path shared by a Swap-menu entry and a
  // one-click offer button. POSTs api/chat/<id>/swap with swapBody(option) (piece 7's
  // route validates the account label server-side and returns the updated session
  // with its new marker row in the transcript); setSession + render show it.
  async performSwap(option) {
    const session = this.session;
    if (!session) return;
    if (this.isSending) {
      showToast('Wait for the current reply to finish before switching model.', 'info');
      return;
    }
    this.setSwapOpen(false);
    this.swapBtn.disabled = true;
    try {
      const resp = await fetch(`api/chat/${encodeURIComponent(session.id)}/swap`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(swapBody(option)),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.description || `HTTP ${resp.status}`);
      this.pendingSwapOffer = null; // the swap succeeded; the offer is stale now
      this.setSession(body);       // updated session -- the marker row renders with it
    } catch (e) {
      showToast('Could not switch model: ' + e.message);
    } finally {
      this.swapBtn.disabled = false;
    }
    this.render();
  }

  // Fetches the pane's CURRENT session (by remembered id), falling back to the most
  // recently used chat when it has none yet or the remembered one is gone. Used on page
  // load and for the host's 'agent-manager-chat-refresh' postMessage.
  async load() {
    let session = null;
    if (this.sessionId) {
      try { session = await fetchJson(`api/chat/${encodeURIComponent(this.sessionId)}`); } catch (e) { session = null; }
    }
    if (!session) {
      try { session = await fetchJson('api/chat/active'); } catch (e) { session = null; }
    }
    this.setSession(session);
    this.render();
  }

  // Loads a specific chat into the pane (switcher pick). Keeps the current one on failure.
  async loadSession(id) {
    if (this.isSending) {
      showToast('Wait for the current reply to finish before switching chats.', 'info');
      return false;
    }
    try {
      this.setSession(await fetchJson(`api/chat/${encodeURIComponent(id)}`));
    } catch (e) {
      showToast('Could not load that chat: ' + e.message);
      return false;
    }
    this.render();
    return true;
  }

  async startNew() {
    if (this.isSending) {
      showToast('Wait for the current reply to finish before starting a new chat.', 'info');
      return false;
    }
    let ok = false;
    try {
      const resp = await fetch('api/chat/new', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: this.providerChoice }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.description || `HTTP ${resp.status}`);
      this.setSession(body);
      ok = true;
    } catch (e) {
      showToast('Could not start a new conversation: ' + e.message);
    }
    this.render();
    return ok;
  }

  async onRootClick(e) {
    // HUB0008 10/11: Swap-menu entries and one-click offer buttons both resolve to a
    // data-swap-index into this.swapChoices and share performSwap (same POST body path
    // as the header menu). Checked before the template-card handler below.
    const swapEl = e.target.closest &&
      (e.target.closest('.chat-swap-menu [data-swap-index]') || e.target.closest('.chat-swap-offer-btn'));
    if (swapEl) {
      const option = this.swapChoices[Number(swapEl.dataset.swapIndex)];
      if (option) { this.setSwapOpen(false); this.performSwap(option); }
      return;
    }
    const btn = e.target.closest && e.target.closest('.tpl-save');
    if (!btn || !this.session || !this.root.contains(btn)) return;
    const card = btn.closest('.tpl-card');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      const resp = await fetch(`api/chat/${encodeURIComponent(this.session.id)}/template/save`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          entry: Number(card.dataset.entry), index: Number(card.dataset.tpl),
          key: card.querySelector('.tpl-key').value, text: card.querySelector('.tpl-text').value,
        }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.description || `HTTP ${resp.status}`);
      this.setSession(body.session);
      showToast(body.proposal.message, body.proposal.status === 'saved' ? 'info' : 'error');
    } catch (err) {
      showToast('Could not save template: ' + err.message);
    }
    this.render();
  }

  // Context Log panel (2026-09-15, see /home/wok/.claude/plans/immutable-noodling-axolotl.md's
  // Phase 5): shows this session's own compacted, durable record -- a plain markdown file
  // context-log-sweep.js (core repo) appends to as the conversation goes, under
  // "Agent Manager Reports/Chat Context Logs/<sessionId>.md" in the Second Brain vault.
  //
  // The fetch below is deliberately an ABSOLUTE path (`/api/second-brain/file?...`), unlike
  // every other fetch()/href/src in this file -- this page is reached through the host
  // dashboard's generic proxy (see this file's own header and chat.html's comment), so a
  // RELATIVE path would resolve against THIS PLUGIN's own routes (forwarded to port 7442,
  // which has no /api/second-brain/* route at all). /api/second-brain/file lives on the
  // CORE dashboard itself (python/dashboard/routes/second_brain.py) -- an absolute path
  // resolves against the proxy's own origin (the host dashboard, same origin the iframe is
  // served through), reaching that real route directly, bypassing the plugin-proxy prefix
  // entirely. This is the one deliberate exception to "always relative" in this file.
  contextLogNotePath() {
    return `Agent Manager Reports/Chat Context Logs/${this.session.id}.md`;
  }

  async showContextLog() {
    if (!this.session) return;
    this.logPanel.hidden = false;
    this.logContent.textContent = 'Loading...';
    try {
      const url = `/api/second-brain/file?path=${encodeURIComponent(this.contextLogNotePath())}`;
      const body = await fetchJson(url);
      this.logContent.textContent = body.content || '(empty)';
    } catch (e) {
      // A 404 here just means nothing has been swept into this session's note yet -- a
      // normal, expected state for a short or brand-new conversation, not an error worth
      // alarming over. Non-404 failures (500, network error, etc.) are logged and shown
      // with a distinct message so the user knows the service is actually broken.
      const is404 = (e && e.status === 404) ||
                    (e && typeof e.message === 'string' && /\b404\b/.test(e.message));
      if (is404) {
        this.logContent.textContent = 'No context log entries yet for this session.';
      } else {
        console.error('showContextLog: failed to load context log', e);
        this.logContent.textContent = 'Failed to load context log. Please try again.';
      }
    }
  }

  async send() {
    if (this.isSending) return;
    const message = this.input.value.trim();
    if (!message || !this.session) return;
    const session = this.session; // a mid-stream switch is blocked, but never write into another chat
    this.pendingSwapOffer = null; // a new turn supersedes any previous failed-turn offer
    this.isSending = true;
    this.input.value = '';
    this.input.disabled = true;
    this.sendBtn.disabled = true;
    this.sendBtn.textContent = 'Thinking...';
    session.transcript.push({ role: 'user', text: message });
    const assistantEntry = { role: 'assistant', text: '' };
    session.transcript.push(assistantEntry);
    this.render();
    this.onSessionChange(session); // lets the switcher show this chat as busy
    try {
      // Accept: text/event-stream is REQUIRED here, not cosmetic -- the host dashboard's
      // generic plugin proxy (routes/plugin_proxy.py) only takes its true streaming
      // passthrough path (requests+stream=True, piped chunk by chunk) when BOTH the
      // plugin's own plugins.json entry declares proxy.sse:true AND the client's request
      // carries this header; missing it falls through to the proxy's non-SSE path, which
      // reads the WHOLE upstream response into memory (urllib's r.read()) before ever
      // replying -- the connection this repo's own /api/chat/<id>/message route always
      // streams from gets fully buffered one hop upstream, so every response arrives in
      // one lump at the end instead of trickling in as the model actually generates it.
      // Root-caused live 2026-09-22 (Grimmethy, through the dashboard's proxied iframe --
      // this bug never showed hitting the plugin directly on :7442, only through :7420's
      // proxy): this fetch() had no Accept header at all.
      const resp = await fetch(`api/chat/${encodeURIComponent(session.id)}/message`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({ message }),
      });
      if (!resp.ok || !resp.body) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.description || `HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let finalSession = null;
      let streamErr = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const frames = buf.split('\n\n');
        buf = frames.pop();
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data: '));
          if (!line) continue;
          const evt = JSON.parse(line.slice(6));
          if (evt.type === 'preempt') {
            const lanes = evt.lanes || [];
            const killed = lanes.filter((l) => l.action === 'killed').map((l) => l.lane);
            const spared = lanes.filter((l) => l.action === 'spared')
              .map((l) => `${l.lane} (${Math.round((l.ageSeconds || 0) / 60)}m in)`);
            const bits = [];
            if (killed.length) bits.push('freed ' + killed.join(', '));
            if (spared.length) bits.push('spared ' + spared.join(', '));
            if (bits.length) { assistantEntry.text = `⚡ ${bits.join(' · ')}\n\n`; this.render(); }
          } else if (evt.type === 'chunk') {
            assistantEntry.text += evt.text;
            this.render();
          } else if (evt.type === 'final') {
            finalSession = evt.session;
          } else if (evt.type === 'error') {
            streamErr = evt.error;
            // HUB0008 10/11: a failed CLAUDE turn classified as a limit error carries
            // swapOffer {kind, options} on the SAME error event (routes/chat.py, piece
            // 8). Hold it so render() shows the one-click offer; absent on every other
            // error (local turns, unclassified) -- then nothing extra is rendered.
            this.pendingSwapOffer = evt.swapOffer || null;
          }
        }
      }
      if (streamErr) throw new Error(streamErr);
      if (finalSession) this.setSession(finalSession);
    } catch (e) {
      showToast('Chat message failed: ' + e.message);
      // The server persists stream errors into the transcript; re-read it so the saved
      // entry shows. If it couldn't (dropped connection, server down) keep the local
      // transcript and write the error into it ourselves -- never let it only flash.
      let refreshed = null;
      try { refreshed = await fetchJson(`api/chat/${encodeURIComponent(session.id)}`); } catch (e2) { /* fall back below */ }
      const last = refreshed && refreshed.transcript && refreshed.transcript[refreshed.transcript.length - 1];
      if (refreshed && refreshed.id === session.id && last && last.error) {
        this.setSession(refreshed);
      } else {
        assistantEntry.text = (assistantEntry.text ? assistantEntry.text + '\n\n' : '') + 'Error: ' + e.message;
        assistantEntry.error = true;
      }
    } finally {
      this.isSending = false;
      this.input.disabled = false;
      this.sendBtn.disabled = false;
      this.sendBtn.textContent = 'Send';
    }
    this.render();
    this.onSessionChange(this.session);
  }

  async toggleReserve() {
    if (!this.session) return;
    const btn = this.reserveBtn;
    const wantOn = !this.session.reserved;
    btn.disabled = true;
    // No client-side timeout, deliberately -- reserving ON can genuinely block
    // server-side (the core dashboard's own acquire() waiting on a busy worker lane).
    btn.textContent = wantOn ? 'Reserving...' : 'Releasing...';
    try {
      const resp = await fetch(`api/chat/${encodeURIComponent(this.session.id)}/reserve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: wantOn }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.description || `HTTP ${resp.status}`);
      this.setSession(body);
    } catch (e) {
      showToast('Could not toggle reservation: ' + e.message);
    } finally {
      btn.disabled = false;
    }
    this.render();
  }

  render() {
    const session = this.session;
    this.syncSwapHeader();
    if (session && session.provider && this.providerChoice !== session.provider) {
      this.providerChoice = session.provider;
      this.syncProviderToggle();
    }

    const showReserve = !!session && session.provider === 'local';
    this.reserveBtn.style.display = showReserve ? '' : 'none';
    if (showReserve) {
      this.reserveBtn.classList.toggle('active', !!session.reserved);
      this.reserveBtn.textContent = session.reserved ? 'Reserved' : 'Reserve';
      this.reserveBtn.title = session.reserved
        ? 'Holding the local model exclusively -- click to release it back to the worker lanes'
        : 'Hold the local model exclusively for this conversation until you turn it back off';
    }

    this.transcriptEl.innerHTML = session && session.transcript.length
      ? chatRenderTranscript(session.transcript, session)
      : '<div class="empty">Say something to the chat.</div>';
    // HUB0008 10/11: the failed-turn one-click swap offer. offerText (swap-view, piece 9)
    // decides whether there is anything to offer at all -- null/missing offer, unknown
    // kind or an empty options list all yield '' and we render NOTHING extra. When it
    // speaks, one button per option, each wired (via onRootClick) to the same
    // performSwap POST as the header Swap menu. Appended INSIDE the transcript scroll
    // box when one exists so it scrolls with the failed turn.
    if (this.pendingSwapOffer) {
      const offer = this.pendingSwapOffer;
      const text = offerText(offer);
      const options = Array.isArray(offer.options) ? offer.options : [];
      if (text && options.length) {
        this.swapChoices = options;
        const html =
          `<div class="chat-swap-offer">` +
          `<div class="chat-swap-offer-text">${escapeHtml(text)}</div>` +
          `<div class="chat-swap-offer-actions">` +
          options.map((o, i) =>
            `<button type="button" class="secondary chat-swap-offer-btn" data-swap-index="${i}">` +
            `Switch to ${escapeHtml(swapOptionLabel(o))}</button>`).join('') +
          `</div></div>`;
        const target = this.transcriptEl.querySelector('.chat-transcript') || this.transcriptEl;
        target.insertAdjacentHTML('beforeend', html);
      }
    }
    const scrollBox = this.transcriptEl.querySelector('.chat-transcript');
    if (scrollBox) scrollBox.scrollTop = scrollBox.scrollHeight;
  }
}

// Session switcher: the header dropdown listing GET api/chat/sessions. Pure formatting
// lives in sessions-view.js; this class only does DOM + fetches. The list re-fetches every
// 5s while the dropdown is open and after any action.
class SessionSwitcher {
  constructor(slot, { getSelectedId, onSelect, onNew, onClose, getRoots }) {
    this.slot = slot;
    this.getSelectedId = getSelectedId;
    this.onSelect = onSelect;
    this.onNew = onNew;
    this.onClose = onClose;
    this.getRoots = getRoots || (() => []);
    this.rows = [];
    // Read-only terminal sessions from the optional session agent (see agent_client.py).
    this.terminal = { enabled: false, reachable: false, rows: [], hosts: [], skipped: [] };
    // One EventSource per host (HUB0017), keyed by host name; '' is the single-host
    // fallback stream. Closed together by stopEvents().
    this.hostEvents = {};
    this._eventSignature = null;
    // Terminal-section host filter: '' means All.
    this.hostFilter = '';
    this.open = false;
    this.pollTimer = null;
    this.slot.innerHTML = `
      <button type="button" class="secondary chat-switcher-btn" aria-haspopup="listbox" aria-expanded="false"></button>
      <div class="chat-switcher-menu" hidden></div>`;
    this.btn = this.slot.querySelector('.chat-switcher-btn');
    this.menu = this.slot.querySelector('.chat-switcher-menu');
    this.btn.onclick = (e) => { e.stopPropagation(); this.setOpen(!this.open); };
    this.menu.addEventListener('click', (e) => this.onMenuClick(e));
    document.addEventListener('click', (e) => { if (this.open && !this.slot.contains(e.target)) this.setOpen(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.open) this.setOpen(false); });
    this.renderButton();
  }

  setOpen(open) {
    this.open = open;
    this.menu.hidden = !open;
    this.btn.setAttribute('aria-expanded', String(open));
    clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.stopEvents();
    if (open) {
      this.refresh().then(() => { if (this.open) this.startEvents(); });
      this.pollTimer = setInterval(() => this.refresh(), 5000);
    }
  }

  // Live terminal-session updates while the dropdown is open. One EventSource per host
  // (HUB0017); each reconnects by itself, and the 5s refresh() above is the fallback
  // when a stream is down. A single host uses the plain single-host URL.
  startEvents() {
    if (!this.terminal.enabled || !this.terminal.reachable || typeof EventSource === 'undefined') return;
    const hosts = (Array.isArray(this.terminal.hosts) && this.terminal.hosts.length)
      ? this.terminal.hosts
      : hostsOf(this.terminal.rows);
    if (!hosts.length) return;
    const signature = hosts.join('\u0000');
    if (this._eventSignature === signature) return; // streams already match the host list
    this.stopEvents(); // host list changed: close all, reopen fresh
    this._eventSignature = signature;
    if (hosts.length === 1) {
      this.openEventStream(hosts[0], 'api/chat/terminal-sessions/events');
    } else {
      for (const host of hosts) {
        this.openEventStream(host, 'api/chat/terminal-sessions/events?host=' + encodeURIComponent(host));
      }
    }
  }

  openEventStream(host, url) {
    const events = new EventSource(url);
    this.hostEvents[host] = events;
    for (const name of ['session.updated', 'session.removed']) {
      events.addEventListener(name, (e) => {
        let data = null;
        try { data = JSON.parse(e.data); } catch (err) { return; }
        this.terminal.rows = applyTerminalEvent(this.terminal.rows, name, data);
        this.render();
      });
    }
  }

  stopEvents() {
    for (const host of Object.keys(this.hostEvents)) this.hostEvents[host].close();
    this.hostEvents = {};
    this._eventSignature = null;
  }

  async refresh() {
    try {
      this.rows = orderSessions(await fetchJson('api/chat/sessions'));
    } catch (e) {
      // Keep the last known rows; the dropdown just goes stale until the next tick.
    }
    try {
      const t = await fetchJson('api/chat/terminal-sessions');
      this.terminal = {
        enabled: !!t.enabled,
        reachable: !!t.reachable,
        rows: Array.isArray(t.sessions) ? t.sessions : [],
        hosts: Array.isArray(t.hosts) ? t.hosts : [],
        skipped: Array.isArray(t.skipped) ? t.skipped : [],
      };
    } catch (e) {
      // Same: keep what we had.
    }
    // Host list can change between polls (agent added/removed): re-sync the streams.
    if (this.open) this.startEvents();
    this.render();
    return this.rows;
  }

  renderButton() {
    const row = this.rows.find((r) => r.id === this.getSelectedId());
    this.btn.textContent = (row ? truncateTitle(row.title) : 'Chats') + ' ▾';
    const roots = this.getRoots();
    this.btn.title = roots.length
      ? 'Switch chat. Accessible repos (roots[0] is primary):\n' + roots.join('\n')
      : 'Switch chat';
  }

  // One dropdown row. `fmt.context` is the reserved slot for a later context-token figure
  // (plugin chats do not report one yet) -- render it here when sessions-view.js starts
  // returning a value.
  renderRow(fmt, selected) {
    const contextHtml = fmt.context ? `<span class="chat-switcher-context">${escapeHtml(fmt.context)}</span>` : '';
    return `<div class="chat-switcher-row${selected ? ' selected' : ''}" role="option" data-id="${escapeHtml(fmt.id)}" aria-selected="${selected}">
      <div class="chat-switcher-main" title="${escapeHtml(fmt.fullTitle)}">
        <div class="chat-switcher-title">${escapeHtml(fmt.title)}</div>
        <div class="chat-switcher-meta">
          <span>${escapeHtml(fmt.provider)}</span>
          <span class="chat-switcher-status${fmt.busy ? ' busy' : ''}">${escapeHtml(fmt.status)}</span>
          <span>${escapeHtml(fmt.timer)}</span>
          <span>${escapeHtml(fmt.turns)}</span>${contextHtml}
        </div>
      </div>
      <button type="button" class="secondary chat-switcher-close" title="Close this chat" aria-label="Close this chat">×</button>
    </div>`;
  }

  // A terminal session row: display only (no select, no close) until a handoff exists.
  // `showHost` gates the host badge: pointless when every row shares one host.
  renderTerminalRow(fmt, showHost) {
    const ctx = fmt.context ? `<span class="chat-switcher-context">${escapeHtml(fmt.context)}</span>` : '';
    const step = fmt.step ? `<div class="chat-switcher-step">${escapeHtml(fmt.step)}</div>` : '';
    const badge = (showHost && fmt.host)
      ? `<span class="terminal-row-host-badge">${escapeHtml(fmt.host)}</span>`
      : '';
    return `<div class="chat-switcher-trow" title="${escapeHtml(fmt.fullTitle)}">
      <div class="chat-switcher-title">${escapeHtml(fmt.title)}${badge}</div>
      <div class="chat-switcher-meta">
        <span class="chat-switcher-status${fmt.busy ? ' busy' : ''}${fmt.waiting ? ' waiting' : ''}">${escapeHtml(fmt.status)}</span>
        <span>${escapeHtml(fmt.timer)}</span>${ctx}
      </div>${step}
    </div>`;
  }

  renderTerminalSection(now) {
    const t = this.terminal;
    if (!t.enabled) return '';
    const rows = orderTerminalSessions(t.rows);
    // Host badge + All/each-host filter only make sense with more than one host.
    const hosts = hostsOf(rows);
    const multiHost = hosts.length > 1;
    // A stale filter (e.g. its host vanished from the list) would empty the rows; clamp.
    let selectedHost = multiHost ? this.hostFilter : '';
    if (selectedHost && !hosts.includes(selectedHost)) selectedHost = '';
    let body;
    if (!t.reachable) body = '<div class="chat-switcher-empty">Session agent unreachable</div>';
    else if (!t.rows.length) body = '<div class="chat-switcher-empty">No terminal sessions</div>';
    else body = filterByHost(rows, selectedHost)
      .map((r) => this.renderTerminalRow(formatTerminalRow(r, now), multiHost)).join('');
    let filterHtml = '';
    if (multiHost) {
      const options = [''].concat(hosts);
      filterHtml = '<div class="chat-switcher-host-filter">'
        + options.map((h) => {
            const active = (selectedHost || '') === (h || '');
            return `<button type="button" class="secondary chat-switcher-host-filter-btn${active ? ' active' : ''}" data-host-filter="${escapeHtml(h || '')}">${h ? escapeHtml(h) : 'All'}</button>`;
          }).join('')
        + '</div>';
    }
    const skippedText = skippedLabel(t.skipped);
    const skippedLine = skippedText
      ? `<div class="chat-switcher-empty">Some hosts were skipped: ${escapeHtml(skippedText)}</div>`
      : '';
    return `<div class="chat-switcher-group">Terminal sessions · read-only</div>${filterHtml}${body}${skippedLine}`;
  }

  render() {
    this.renderButton();
    const now = Date.now();
    const selectedId = this.getSelectedId();
    this.menu.innerHTML = `<div class="chat-switcher-new" role="option">+ New chat</div>`
      + this.rows.map((r) => this.renderRow(formatRow(r, now), r.id === selectedId)).join('')
      + this.renderTerminalSection(now);
  }

  async onMenuClick(e) {
    e.stopPropagation();
    // Host filter (terminal section): pick All / one host and re-render only.
    const hostFilterBtn = e.target.closest('.chat-switcher-host-filter-btn');
    if (hostFilterBtn) {
      this.hostFilter = hostFilterBtn.dataset.hostFilter || '';
      this.render();
      return;
    }
    if (e.target.closest('.chat-switcher-new')) {
      this.setOpen(false);
      await this.onNew();
      await this.refresh();
      return;
    }
    const row = e.target.closest('.chat-switcher-row');
    if (!row) return;
    const id = row.dataset.id;
    if (e.target.closest('.chat-switcher-close')) {
      await this.onClose(id);
      await this.refresh();
      return;
    }
    this.setOpen(false);
    await this.onSelect(id);
    await this.refresh();
  }
}

function mountChat() {
  if (window.__CHAT_MULTI_PANES__) return;
  // #chat-panel (in templates/chat.html) is the sole id-based contract between
  // this file and the template; every other selector here is class-based.
  const root = document.getElementById('chat-panel');
  if (!root) {
    const msg = 'Chat failed to mount: #chat-panel not found in the template';
    console.error(msg);
    const err = document.createElement('div');
    err.className = 'chat-mount-error';
    err.textContent = msg;
    // Minimal inline styling so the message is readable even if chat.css never loads.
    err.style.cssText =
      'padding:16px 24px;margin:24px auto;max-width:480px;' +
      'border:2px solid #c00;border-radius:6px;background:#fff0f0;' +
      'color:#900;font:14px/1.5 system-ui,sans-serif;text-align:center;';
    document.body.appendChild(err);
    return;
  }
  let switcher = null;
  const pane = new ChatPane(root, { onSessionChange: () => { if (switcher) switcher.refresh(); } });
  switcher = new SessionSwitcher(pane.switcherSlot, {
    getSelectedId: () => pane.sessionId,
    getRoots: () => (pane.session && Array.isArray(pane.session.roots) ? pane.session.roots : []),
    onSelect: (id) => (id === pane.sessionId ? true : pane.loadSession(id)),
    onNew: () => pane.startNew(),
    onClose: async (id) => {
      if (id === pane.sessionId && pane.isSending) {
        showToast('Wait for the current reply to finish before closing this chat.', 'info');
        return;
      }
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
      if (id !== pane.sessionId) return;
      const rows = await switcher.refresh(); // already excludes the closed chat
      const next = nextSelectionAfterClose(rows, id);
      if (next) await pane.loadSession(next); else await pane.startNew();
    },
  });
  pane.load().then(() => switcher.refresh());

  // The host dashboard's sendTextToChat() (core-ui.js) POSTs directly to our own
  // api/chat/inject, then postMessages this page's iframe rather than calling into it --
  // separate documents/origins, so a postMessage listener is the only way back. No origin
  // check: the host serves us through its own same-origin proxy path, and the payload
  // carries no sensitive data, just "go re-fetch your own current session." Skipped while
  // a reply is streaming: replacing the session mid-stream would orphan the stream.
  window.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'agent-manager-chat-refresh' && !pane.isSending) {
      pane.load();
    }
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountChat);
else mountChat();

export { ChatPane, SessionSwitcher, showToast };
