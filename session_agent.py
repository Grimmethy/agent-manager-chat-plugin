#!/usr/bin/env python3
"""Session agent: discovers local Claude Code terminal sessions and serves them over HTTP.

Stdlib only. Per session it reports status, current step, context size and a busy/idle
timer source, and it streams changes over Server-Sent Events. Sources:
  * `claude agents --json --all` (documented listing; optional, registry files are the fallback)
  * ~/.claude/sessions/<pid>.json (UNDOCUMENTED registry; every key is optional here)
  * ~/.claude/projects/*/<sessionId>.jsonl (transcripts; tailed by byte offset, never re-read)
Transcripts are private: loopback bind by default, a non-loopback bind needs a token, and
no message text is ever logged.
"""
import argparse
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

MAX_LINE = 1024 * 1024  # transcript lines larger than this (api-request-blob...) are skipped unparsed
TEXT_CAP = 2000
STEP_CAP = 40
DEFAULT_LIMIT = 200
STATUSES = ("busy", "idle", "waiting")
# Background sessions report CLI state "blocked" (status still "busy"): needs you -> waiting.
# Registry status "shell" was seen once, unexplained, so it is NOT mapped (-> idle).
NEEDS_YOU = ("blocked",)
KIND_ALIASES = {"bg": "background"}  # registry says "bg", the CLI says "background"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
CURSOR_RE = re.compile(r"^o(\d+)$")


def iso_from_ms(ms):
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def map_status(*raw):
    """busy|idle|waiting from the raw (status, state) values. A needs-you
    value anywhere wins; otherwise the first recognised value; anything else is idle."""
    vals = [v.lower() for v in raw if isinstance(v, str)]
    if any(v in NEEDS_YOU for v in vals):
        return "waiting"
    return next((v for v in vals if v in STATUSES), "idle")


def pid_alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_statusline_window(directory, session_id, model=None):
    """contextWindowSize from <dir>/<sessionId>.json (written by a status line command). A
    window never changes within a session, so the file does not expire; it is ignored only when
    malformed or when its recorded model differs from the transcript's latest model."""
    if not directory or not SESSION_ID_RE.match(session_id):
        return None
    try:
        with open(Path(directory) / f"{session_id}.json", "rb") as f:
            data = json.loads(f.read(4096))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    size = data.get("contextWindowSize")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if isinstance(data.get("model"), str) and isinstance(model, str) and data["model"] != model:
        return None  # the model was switched since the file was written
    return size


def load_windows(path):
    """JSON {model-id substring: window size}. Missing file -> error; bad entries dropped."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("windows config must be a JSON object")
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool) and v > 0}


def window_for(model, table):
    if not isinstance(model, str):
        return None
    for key in sorted(table, key=len, reverse=True):
        if key in model:
            return table[key]
    return None


def one_line(text, cap):
    return " ".join(str(text).split())[:cap]


def tool_summary(block):
    """'Bash: <first 40 chars>' for a tool_use block."""
    name = block.get("name") if isinstance(block.get("name"), str) else "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    for key in ("command", "file_path", "pattern", "path", "url", "description"):
        if isinstance(inp.get(key), str) and inp[key].strip():
            return f"{name}: {one_line(inp[key], STEP_CAP)}"
    return name


def entries_of(obj):
    """Role + text/tool summaries for one transcript object (sidechains excluded)."""
    if not isinstance(obj, dict) or obj.get("isSidechain") is True:
        return []
    kind, msg = obj.get("type"), obj.get("message")
    if kind not in ("user", "assistant") or not isinstance(msg, dict):
        return []
    ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            out.append({"role": kind, "text": block["text"][:TEXT_CAP], "ts": ts})
        elif block.get("type") == "tool_use" and kind == "assistant":
            out.append({"role": "tool", "text": tool_summary(block), "ts": ts})
    return out


def iter_lines(path, offset):
    """Yield (object|None, end_offset) per complete line from `offset`. Partial trailing
    lines wait for the next call; oversized or corrupt lines yield None."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        f.seek(offset)
        while True:
            raw = f.readline(MAX_LINE + 1)
            if not raw:
                return
            oversized = len(raw) > MAX_LINE and not raw.endswith(b"\n")
            if oversized:
                while not raw.endswith(b"\n"):
                    raw = f.readline(MAX_LINE)
                    if not raw:
                        return  # still being written; retry from the line start
            elif not raw.endswith(b"\n"):
                return
            offset = f.tell()
            obj = None
            if not oversized and (b'"assistant"' in raw or b'"user"' in raw):
                try:
                    obj = json.loads(raw)
                except ValueError:
                    obj = None
            yield (obj if isinstance(obj, dict) else None), offset


def read_registry(home):
    out = []
    try:
        names = sorted(os.listdir(home / "sessions"))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(home / "sessions" / name, "rb") as f:
                data = json.loads(f.read(65536))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("pid") is None and name[:-5].isdigit():
            data["pid"] = int(name[:-5])
        out.append(data)
    return out


def run_claude_agents():
    try:
        res = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True,
                             text=True, timeout=10, check=True)
        data = json.loads(res.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else None


class TranscriptState:
    """Incremental view of one session's transcript."""

    def __init__(self, home, session_id):
        self.home, self.session_id = home, session_id
        self.path, self.offset, self.ready = None, 0, False
        self._reset()

    def _reset(self):
        self.context = self.model = self.step = self.last_ts = None

    def _find(self):
        for p in sorted((self.home / "projects").glob(f"*/{self.session_id}.jsonl")):
            return p
        return None

    def apply(self, obj):
        if obj.get("isSidechain") is True:
            return
        if isinstance(obj.get("timestamp"), str):
            self.last_ts = obj["timestamp"]
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        if msg.get("model") == "<synthetic>":
            return  # client-generated placeholder with zero usage: not a real API response
        if obj.get("type") == "user":
            if any(e["role"] == "user" for e in entries_of(obj)):
                self.step = None  # a new prompt starts a fresh turn
        elif obj.get("type") == "assistant":
            usage = msg.get("usage")
            if isinstance(usage, dict):
                nums = [usage.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                               "cache_creation_input_tokens")]
                self.context = sum(n for n in nums if isinstance(n, int) and not isinstance(n, bool))
            if isinstance(msg.get("model"), str):
                self.model = msg["model"]
            for e in entries_of(obj):
                if e["role"] == "tool":
                    self.step = e["text"]

    def poll(self):
        """Consume new lines; return new entries (empty on the initial catch-up scan)."""
        if self.path is None:
            self.path = self._find()
            if self.path is None:
                return []
        try:
            size = self.path.stat().st_size
        except OSError:
            self.path = None
            return []
        if size < self.offset:  # truncated or replaced
            self.offset, self.ready = 0, False
            self._reset()
        if size == self.offset:
            self.ready = True
            return []
        entries = []
        for obj, end in iter_lines(self.path, self.offset):
            self.offset = end
            if obj is not None:
                self.apply(obj)
                entries.extend(entries_of(obj))
        first, self.ready = not self.ready, True
        return [] if first else entries


def read_transcript(path, cursor, limit):
    """Entries after byte-offset `cursor`; returns (entries, next_offset)."""
    entries, nxt = [], cursor
    for obj, end in iter_lines(path, cursor):
        es = entries_of(obj) if obj is not None else []
        if entries and len(entries) + len(es) > limit:
            break
        entries.extend(es)
        nxt = end
        if len(entries) >= limit:
            break
    return entries, nxt


# TODO(peer-message): UNVERIFIED against a real Claude Code session. The wire format of the
# inbox socket is undocumented. Only an optional first line {"type":"auth","token":"..."}
# (for the session's own child processes) is documented, and the socket drops a connection that
# has not sent a complete line within 30 s. The single JSON line below is a PLACEHOLDER, not a
# known format. Verify against a real session before enabling --allow-send anywhere:
#   1. the real message envelope (field names, whether "text" is right, newline framing);
#   2. whether the socket needs the auth line for a non-child sender, and where a token comes from;
#   3. whether it replies (ack/error) and whether it closes after one message;
#   4. how the receiver labels the message (peer messages are "not from the user" and CANNOT
#      approve permission prompts -- this must never be presented as a way to answer one).
def encode_peer_message(text):
    return (json.dumps({"type": "message", "text": text}) + "\n").encode()


def send_peer(socket_path, text, timeout=5.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall(encode_peer_message(text))


class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []

    def subscribe(self, initial=()):
        q = queue.Queue(maxsize=1000)
        for item in initial:
            q.put_nowait(item)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, event, data):
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait((event, data))
                except queue.Full:
                    pass  # slow client: drop rather than block the poller


class Agent:
    def __init__(self, home, windows=None, token=None, host=None, lister=run_claude_agents,
                 alive=pid_alive, cli_ttl=10.0, allow_send=False, allowed_hosts=(),
                 statusline_dir=None):
        self.home, self.windows, self.token = Path(home), windows or {}, token
        self.host = host or socket.gethostname()
        self.lister, self.alive, self.cli_ttl = lister, alive, cli_ttl
        self.hub, self.lock, self.stop = Hub(), threading.Lock(), threading.Event()
        self.rows, self.states, self.metas = {}, {}, {}
        self.allow_send = allow_send
        self.statusline_dir = statusline_dir
        # DNS-rebinding guard: only these Host/Origin names are served (no CORS headers either).
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1"} | {h.lower() for h in allowed_hosts}
        self._cli, self._cli_at = None, 0.0

    def discover(self):
        now = time.monotonic()
        if self.lister and now - self._cli_at >= self.cli_ttl:
            self._cli, self._cli_at = self.lister(), now
        merged = {}
        for src in (read_registry(self.home), self._cli or []):  # CLI (documented) wins
            for d in src:
                sid = d.get("sessionId")
                if isinstance(sid, str) and SESSION_ID_RE.match(sid):
                    merged[sid] = {**merged.get(sid, {}), **d}
        return [d for d in merged.values() if self.alive(d.get("pid"))]

    def build_row(self, meta, st):
        waiting_for = meta.get("waitingFor")
        waiting_for = str(waiting_for)[:80] if waiting_for is not None else None
        status = map_status(meta.get("status"), meta.get("state"))
        if status == "waiting" and waiting_for:
            step = f"Waiting: {waiting_for}"
        elif status == "idle":
            step = None  # the last tool of a finished turn would read as current
        elif st.step:
            step = st.step
        else:
            step = "Thinking" if status == "busy" else None
        since = iso_from_ms(meta.get("statusUpdatedAt")) or st.last_ts or iso_from_ms(meta.get("startedAt"))
        name = meta.get("name")
        return {
            "id": meta["sessionId"],
            "name": name if isinstance(name, str) else None,
            "cwd": meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
            "kind": KIND_ALIASES.get(meta["kind"], meta["kind"]) if isinstance(meta.get("kind"), str) else None,
            "pid": meta.get("pid"),
            "status": status,
            "waitingFor": waiting_for,
            "statusSince": since,
            "lastActivityAt": st.last_ts,
            "step": step,
            "contextTokens": st.context,
            "contextWindow": read_statusline_window(self.statusline_dir, meta["sessionId"], st.model)
                             or window_for(st.model, self.windows),
            "model": st.model,
            "startedAt": iso_from_ms(meta.get("startedAt")),
            "host": self.host,
        }

    def refresh(self):
        new_rows, events, metas = {}, [], {}
        for meta in self.discover():
            sid = meta["sessionId"]
            metas[sid] = meta
            st = self.states.setdefault(sid, TranscriptState(self.home, sid))
            entries = st.poll()
            row = self.build_row(meta, st)
            new_rows[sid] = row
            if entries:
                events.append(("transcript.append",
                               {"id": sid, "entries": entries, "next": f"o{st.offset}"}))
            if row != self.rows.get(sid):
                events.append(("session.updated", row))
        for sid in set(self.rows) - set(new_rows):
            self.states.pop(sid, None)
            events.append(("session.removed", {"id": sid}))
        with self.lock:
            self.rows, self.metas = new_rows, metas
        for ev in events:
            self.hub.publish(*ev)

    def snapshot(self):
        with self.lock:
            return list(self.rows.values())

    def poll_forever(self, interval):
        while not self.stop.wait(interval):
            try:
                self.refresh()
            except Exception:  # never let one bad poll kill the agent; never log content
                pass


def _add_types(d, acc):
    for k, v in d.items():
        acc.setdefault(str(k)[:40], set()).add(type(v).__name__)


def _enum(dicts, key):
    return sorted({str(d[key])[:30] for d in dicts if isinstance(d.get(key), str)})[:20]


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in sorted(x.items())}
    return sorted(x) if isinstance(x, set) else x


def probe(home, lister=run_claude_agents, alive=pid_alive):
    """Shapes-only report for checking the format assumptions against a real install.
    Emits key names, value TYPES, counts and small enums (status/kind); never text, ids,
    paths or other values. Sessions are numbered, not named."""
    home = Path(home)
    cli = lister()
    reg = read_registry(home)
    report = {"cli": None, "registry": {"files": len(reg), "keys": {}, "pidAlive": 0,
                                        "status": _enum(reg, "status"), "kind": _enum(reg, "kind"),
                                        "entrypoint": _enum(reg, "entrypoint")}, "transcripts": []}
    if cli is not None:
        keys = {}
        for d in cli:
            _add_types(d, keys)
        report["cli"] = {"entries": len(cli), "keys": _jsonable(keys), "status": _enum(cli, "status"),
                         "state": _enum(cli, "state"), "kind": _enum(cli, "kind")}
    for d in reg:
        _add_types(d, report["registry"]["keys"])
        report["registry"]["pidAlive"] += bool(alive(d.get("pid")))
    report["registry"]["keys"] = _jsonable(report["registry"]["keys"])
    live = [d for d in reg if alive(d.get("pid")) and isinstance(d.get("sessionId"), str)
            and SESSION_ID_RE.match(d["sessionId"])]
    live.sort(key=lambda d: d.get("startedAt") if isinstance(d.get("startedAt"), (int, float)) else 0)
    for n, d in enumerate(live, 1):
        st = TranscriptState(home, d["sessionId"])
        st.poll()
        t = {"n": n, "found": st.path is not None, "contextTokens": st.context, "model": st.model}
        if st.path is not None:
            types, top, msgk, usagek, blocks = {}, {}, {}, {}, {}
            lines = bad = big = side = 0
            with open(st.path, "rb") as f:
                for raw in f:
                    lines += 1
                    if len(raw) > MAX_LINE:
                        big += 1
                        continue
                    try:
                        o = json.loads(raw)
                    except ValueError:
                        bad += 1
                        continue
                    if not isinstance(o, dict):
                        continue
                    key = str(o.get("type"))[:40]
                    types[key] = types.get(key, 0) + 1
                    if o.get("type") != "assistant":
                        continue
                    side += o.get("isSidechain") is True
                    _add_types(o, top)
                    m = o.get("message") if isinstance(o.get("message"), dict) else {}
                    _add_types(m, msgk)
                    if isinstance(m.get("usage"), dict):
                        _add_types(m["usage"], usagek)
                    for b in m.get("content") if isinstance(m.get("content"), list) else []:
                        if isinstance(b, dict):
                            blocks.setdefault(str(b.get("type"))[:40], set()).update(
                                str(k)[:40] for k in b)
            t.update(lines=lines, corrupt=bad, oversized=big, lineTypes=types, sidechainAssistantLines=side,
                     assistantKeys=_jsonable(top), messageKeys=_jsonable(msgk),
                     usageKeys=_jsonable(usagek), blockKeys=_jsonable(blocks))
        report["transcripts"].append(t)
    return report


def host_only(value):
    """Hostname from a Host header or Origin URL (no port, no brackets, lower-case)."""
    value = (value or "").strip()
    if "@" in value:  # userinfo has no place in a Host/Origin; never "parse around" it
        return None
    try:
        return urlsplit(value if "://" in value else "//" + value).hostname
    except ValueError:
        return None


def write_token_file(path):
    """New random token, written atomically with mode 0600 (dir 0700); returns the token."""
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)
    return token


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    agent = None

    def log_message(self, *args):  # request lines can carry ids; keep the agent silent
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        hosts = self.agent.allowed_hosts
        origin = self.headers.get("Origin")
        return host_only(self.headers.get("Host")) in hosts and (origin is None or host_only(origin) in hosts)

    def _authorized(self):
        token = self.agent.token
        if not token:
            return True
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts == ["healthz"]:
            return self._json(200, {"ok": True})
        if parts == ["sessions"]:
            return self._json(200, self.agent.snapshot())
        if parts == ["events"]:
            return self._events(session_only=parse_qs(url.query).get("types") == ["session"])
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "transcript":
            return self._transcript(parts[1], parse_qs(url.query))
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if not (len(parts) == 3 and parts[0] == "sessions" and parts[2] == "peer-message"):
            return self._json(404, {"error": "not found"})
        agent = self.agent
        if not agent.allow_send:
            return self._json(403, {"error": "sending is disabled (start with --allow-send)"})
        with agent.lock:
            meta = agent.metas.get(parts[1])
        path = meta.get("messagingSocketPath") if meta else None
        if not isinstance(path, str) or not os.path.isabs(path):
            return self._json(404, {"error": "unknown session or no inbox socket"})
        try:
            length = int(self.headers.get("Content-Length", ""))
            body = json.loads(self.rfile.read(length)) if 0 < length <= 65536 else None
        except ValueError:
            body = None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            return self._json(400, {"error": "body must be {\"text\": \"...\"}"})
        try:
            send_peer(path, text)
        except OSError:
            return self._json(502, {"error": "could not deliver to the session socket"})
        self._json(200, {"delivered": True,
                         "note": "peer message: the receiver treats it as not from the user; "
                                 "it cannot approve permission prompts"})

    def _transcript(self, sid, qs):
        st = self.agent.states.get(sid)
        if st is None or st.path is None:
            return self._json(404, {"error": "unknown session"})
        since = qs.get("since", [""])[0]
        m = CURSOR_RE.match(since) if since else None
        if since and not m:
            return self._json(400, {"error": "bad cursor"})
        try:
            limit = min(max(int(qs.get("limit", [DEFAULT_LIMIT])[0]), 1), DEFAULT_LIMIT)
        except ValueError:
            return self._json(400, {"error": "bad limit"})
        cursor = int(m.group(1)) if m else 0
        try:
            if cursor > st.path.stat().st_size:
                return self._json(400, {"error": "cursor beyond end of transcript"})
        except OSError:
            return self._json(404, {"error": "unknown session"})
        entries, nxt = read_transcript(st.path, cursor, limit)
        self._json(200, {"entries": entries, "next": f"o{nxt}"})

    def _events(self, session_only=False):
        agent = self.agent
        q = agent.hub.subscribe([("session.updated", r) for r in agent.snapshot()])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last = time.monotonic()
        try:
            while not agent.stop.is_set():
                try:
                    event, data = q.get(timeout=0.5)
                    if session_only and not event.startswith("session."):
                        continue
                    out = f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except queue.Empty:
                    if time.monotonic() - last < 15:
                        continue
                    out = ": keepalive\n\n"
                self.wfile.write(out.encode())
                self.wfile.flush()
                last = time.monotonic()
        except OSError:
            pass
        finally:
            agent.hub.unsubscribe(q)


def make_server(agent, host, port):
    if not is_loopback(host) and not agent.token:
        raise ValueError("a non-loopback bind requires --token (or AM_AGENT_TOKEN)")
    handler = type("BoundHandler", (Handler,), {"agent": agent})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7450)
    ap.add_argument("--token", default=os.environ.get("AM_AGENT_TOKEN"),
                    help="bearer token required on every request; 'auto' generates one into --token-file")
    ap.add_argument("--token-file", default=str(Path.home() / ".local/state/agent-manager/session-agent.token"),
                    help="where --token auto writes the token (mode 0600, new token each start)")
    ap.add_argument("--claude-home", default=str(Path.home() / ".claude"),
                    help="registry/transcript directory; does NOT redirect the `claude agents` listing")
    ap.add_argument("--statusline-dir", default=str(Path.home() / ".local/state/agent-manager/statusline"),
                    help="optional <sessionId>.json files {contextWindowSize, model} from a status line "
                         "command (see statusline_writer.py); overrides --windows unless the model changed")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra Host/Origin hostname to accept (repeatable); localhost/127.0.0.1/::1 always are")
    ap.add_argument("--windows", help="JSON file: model-id substring -> context window size")
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval, seconds")
    ap.add_argument("--allow-send", action="store_true",
                    help="enable POST /sessions/<id>/peer-message (experimental, wire format unverified)")
    ap.add_argument("--probe", action="store_true",
                    help="print a shapes-only report of the local Claude Code data and exit")
    args = ap.parse_args(argv)
    if args.probe:
        print(json.dumps(probe(args.claude_home), indent=2))
        return
    windows = load_windows(args.windows) if args.windows else {}
    if args.token == "auto":
        args.token = write_token_file(args.token_file)
        print(f"session agent: token written to {args.token_file}", file=sys.stderr)
    extra = list(args.allow_host)
    if args.bind not in ("0.0.0.0", "::"):
        extra.append(args.bind)
    agent = Agent(args.claude_home, windows, args.token, allow_send=args.allow_send, allowed_hosts=extra,
                  statusline_dir=args.statusline_dir)
    try:
        srv = make_server(agent, args.bind, args.port)
    except ValueError as e:
        ap.error(str(e))
    agent.refresh()
    threading.Thread(target=agent.poll_forever, args=(args.interval,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop.set()


if __name__ == "__main__":
    main()
