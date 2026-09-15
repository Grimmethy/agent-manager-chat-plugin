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

// Own transcript renderer, deliberately NOT the host's shared grillRenderTranscript()
// (an iframe can't call a function in a different document anyway) -- see this file's
// own header comment.
function chatRenderTranscript(transcript, session) {
  const assistantLabel = session && session.provider === 'claude' ? 'Claude' : 'Local';
  const rows = transcript.map((t) => {
    const roleLabel = t.role === 'assistant' ? assistantLabel : 'You';
    const cls = t.role === 'assistant' ? 'chat-msg-assistant' : 'chat-msg-user';
    let body;
    if (t.timestamp) {
      const m = String(t.timestamp).match(/T(\d{2}):(\d{2}):(\d{2})/);
      const stamp = m ? `${m[1]}:${m[2]}:${m[3]}` : String(t.timestamp).slice(11, 19);
      body = String(t.text).split(/\n{2,}/).map(seg =>
        `<p><span class="chat-seg-ts">${escapeHtml(stamp)}</span>${escapeHtml(seg)}</p>`
      ).join('');
    } else {
      body = escapeHtml(t.text);
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
