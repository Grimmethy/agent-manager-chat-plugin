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

let chatSession = null;
let providerChoice = 'local';
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
  const rows = transcript.map((t) => {
    const roleLabel = t.role === 'assistant' ? assistantLabel : 'You';
    const cls = t.role === 'assistant' ? 'chat-msg-assistant' : 'chat-msg-user';
    const isAssistant = t.role === 'assistant';
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
    return `<div class="${cls}"><div class="chat-msg-role">${escapeHtml(roleLabel)}</div><div class="chat-msg-text">${body}</div></div>`;
  }).join('');
  return `<div class="field-label">Conversation</div><div class="chat-transcript">${rows}</div>`;
}

function renderProviderToggle() {
  const claudeClass = providerChoice === 'claude' ? ' provider-toggle-claude' : '';
  const label = providerChoice === 'claude' ? 'Claude' : 'Local';
  return `<button type="button" class="secondary provider-toggle${claudeClass}" id="chat-provider" data-provider="${providerChoice}" title="Local model vs. your Claude subscription -- click to switch">${label}</button>`;
}

function wireProviderToggle() {
  const btn = document.getElementById('chat-provider');
  if (!btn) return;
  btn.onclick = async (e) => {
    e.stopPropagation();
    providerChoice = providerChoice === 'local' ? 'claude' : 'local';
    btn.dataset.provider = providerChoice;
    btn.textContent = providerChoice === 'claude' ? 'Claude' : 'Local';
    btn.classList.toggle('provider-toggle-claude', providerChoice === 'claude');
    await chatStartNew();
  };
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
function chatContextLogNotePath() {
  return `Agent Manager Reports/Chat Context Logs/${chatSession.id}.md`;
}

async function chatShowContextLog() {
  const panel = document.getElementById('chat-context-log-panel');
  const content = document.getElementById('chat-context-log-content');
  if (!panel || !content || !chatSession) return;
  panel.hidden = false;
  content.textContent = 'Loading...';
  try {
    const url = `/api/second-brain/file?path=${encodeURIComponent(chatContextLogNotePath())}`;
    const body = await fetchJson(url);
    content.textContent = body.content || '(empty)';
  } catch (e) {
    // A 404 here just means nothing has been swept into this session's note yet -- a
    // normal, expected state for a short or brand-new conversation, not an error worth
    // alarming over.
    content.textContent = 'No context log entries yet for this session.';
  }
}

function chatWireContextLogPanel() {
  const btn = document.getElementById('chat-context-log-btn');
  const closeBtn = document.getElementById('chat-context-log-close');
  const panel = document.getElementById('chat-context-log-panel');
  if (btn) btn.onclick = chatShowContextLog;
  if (closeBtn && panel) closeBtn.onclick = () => { panel.hidden = true; };
}

async function chatPanelInit() {
  const newBtn = document.getElementById('chat-new-btn');
  if (newBtn) newBtn.onclick = chatStartNew;
  const sendBtn = document.getElementById('chat-send-btn');
  if (sendBtn) sendBtn.onclick = chatSend;
  const input = document.getElementById('chat-input');
  if (input) {
    input.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); chatSend(); }
    };
  }
  chatWireContextLogPanel();
  try {
    chatSession = await fetchJson('api/chat/active');
  } catch (e) {
    chatSession = null;
  }
  chatRender();
}

async function chatStartNew() {
  const newBtn = document.getElementById('chat-new-btn');
  if (newBtn) { newBtn.disabled = true; newBtn.textContent = 'Starting...'; }
  try {
    const resp = await fetch('api/chat/new', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: providerChoice }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.description || `HTTP ${resp.status}`);
    chatSession = body;
  } catch (e) {
    showToast('Could not start a new conversation: ' + e.message);
  } finally {
    if (newBtn) { newBtn.disabled = false; newBtn.textContent = 'New'; }
  }
  chatRender();
}

async function chatSend() {
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('chat-send-btn');
  const message = input && input.value.trim();
  if (!message || !chatSession) return;
  input.value = '';
  sendBtn.disabled = true;
  sendBtn.textContent = 'Thinking...';
  chatSession.transcript.push({ role: 'user', text: message });
  const assistantEntry = { role: 'assistant', text: '' };
  chatSession.transcript.push(assistantEntry);
  chatRender();
  try {
    const resp = await fetch(`api/chat/${encodeURIComponent(chatSession.id)}/message`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
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
          if (bits.length) { assistantEntry.text = `⚡ ${bits.join(' · ')}\n\n`; chatRender(); }
        } else if (evt.type === 'chunk') {
          assistantEntry.text += evt.text;
          chatRender();
        } else if (evt.type === 'final') {
          finalSession = evt.session;
        } else if (evt.type === 'error') {
          streamErr = evt.error;
        }
      }
    }
    if (streamErr) throw new Error(streamErr);
    if (finalSession) chatSession = finalSession;
  } catch (e) {
    showToast('Chat message failed: ' + e.message);
    try { chatSession = await fetchJson('api/chat/active'); } catch (e2) { /* leave as-is */ }
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = 'Send';
  }
  chatRender();
}

async function chatToggleReserve() {
  if (!chatSession) return;
  const btn = document.getElementById('chat-reserve-btn');
  const wantOn = !chatSession.reserved;
  btn.disabled = true;
  // No client-side timeout, deliberately -- reserving ON can genuinely block
  // server-side (the core dashboard's own acquire() waiting on a busy worker lane).
  btn.textContent = wantOn ? 'Reserving...' : 'Releasing...';
  try {
    const resp = await fetch(`api/chat/${encodeURIComponent(chatSession.id)}/reserve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: wantOn }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.description || `HTTP ${resp.status}`);
    chatSession = body;
  } catch (e) {
    showToast('Could not toggle reservation: ' + e.message);
  } finally {
    btn.disabled = false;
  }
  chatRender();
}

function chatRender() {
  const titleEl = document.getElementById('chat-title');
  if (titleEl && chatSession && Array.isArray(chatSession.roots) && chatSession.roots.length) {
    titleEl.title = 'Accessible repos (roots[0] is primary):\n' + chatSession.roots.join('\n');
  }
  const container = document.getElementById('chat-provider-container');
  if (container && !document.getElementById('chat-provider')) {
    container.innerHTML = renderProviderToggle();
    wireProviderToggle();
  }
  if (chatSession && document.getElementById('chat-provider')) {
    const btn = document.getElementById('chat-provider');
    if (btn.dataset.provider !== chatSession.provider) {
      providerChoice = chatSession.provider;
      btn.dataset.provider = providerChoice;
      btn.textContent = providerChoice === 'claude' ? 'Claude' : 'Local';
      btn.classList.toggle('provider-toggle-claude', providerChoice === 'claude');
    }
  }

  const reserveBtn = document.getElementById('chat-reserve-btn');
  if (reserveBtn) {
    const showReserve = !!chatSession && chatSession.provider === 'local';
    reserveBtn.style.display = showReserve ? '' : 'none';
    if (showReserve) {
      reserveBtn.onclick = chatToggleReserve;
      reserveBtn.classList.toggle('active', !!chatSession.reserved);
      reserveBtn.textContent = chatSession.reserved ? 'Reserved' : 'Reserve';
      reserveBtn.title = chatSession.reserved
        ? 'Holding the local model exclusively -- click to release it back to the worker lanes'
        : 'Hold the local model exclusively for this conversation until you turn it back off';
    }
  }

  const transcriptEl = document.getElementById('chat-transcript');
  if (transcriptEl) {
    transcriptEl.innerHTML = chatSession && chatSession.transcript.length
      ? chatRenderTranscript(chatSession.transcript, chatSession)
      : '<div class="empty">Say something to the chat.</div>';
    const scrollBox = transcriptEl.querySelector('.chat-transcript');
    if (scrollBox) scrollBox.scrollTop = scrollBox.scrollHeight;
  }
}

document.addEventListener('DOMContentLoaded', chatPanelInit);

// The host dashboard's sendTextToChat() (core-ui.js) POSTs directly to our own
// api/chat/inject, then postMessages this page's iframe rather than calling into it --
// separate documents/origins, so a postMessage listener is the only way back. No origin
// check: the host serves us through its own same-origin proxy path, and the payload
// carries no sensitive data, just "go re-fetch your own active session."
window.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'agent-manager-chat-refresh') {
    chatPanelInit();
  }
});
