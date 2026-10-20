# Observability Fix Candidates

### AC-1 · Silent SSE relay disconnect hides mid-stream failures
Strength: Strong
Files: routes/chat.py
Snippet:
```
        abort(404, description="session agent is not configured")
    try:
        upstream = agent_client.open_events()
    except agent_client.AgentError:
        abort(502, description="session agent is unreachable")

    def relay():
```

Problem:
The `relay()` generator in `api_terminal_sessions_events` catches `requests.RequestException` and `OSError` with a bare `pass`. The docstring documents the intent — the stream ends and the browser's `EventSource` reconnects — and that intent is correct for the benign "agent went away" case. However, the same `except` clause is also the only place a genuinely abnormal mid-stream failure (a partial read, a corrupted chunk, a timeout that is not a clean peer close) can surface, and the `pass` erases it completely. There is no log line, no distinction between "expected disconnect, client will reconnect" and "something else broke and the client is about to reconnect into the same broken state." An operator looking at server logs sees nothing; the stream simply stops, the client retries, and the failure is invisible on the server side. This is a real observability gap, not a style preference.

Solution:
Replace the bare `pass` with a `logger.warning` call that records the exception type and message, using the stdlib `logging` module (`logging.getLogger(__name__)`) — the same primitive the rest of the Python code in this project already uses. The `except` block still swallows the exception (no rethrow), because the HTTP response body has already begun streaming and a rethrow here would only abort an in-flight response that the caller cannot act on; the browser's `EventSource` reconnect is the correct recovery path. The log line is at `WARNING` level (not `ERROR`) because the common case is a benign disconnect, but it carries enough context (`type(exc).__name__` plus `str(exc)`) that a genuine failure is distinguishable in the log stream. No metric, counter, or gauge is added because this project has no metrics system.

Benefits:
Once the log line is in place, every time the SSE relay stream ends abnormally, the server log records *what* exception occurred and *why*, so an operator can immediately tell a routine agent restart (e.g. `ConnectionError: Remote end closed connection without response`) apart from a real problem (e.g. `ChunkedEncodingError: Invalid chunk header` or a `Timeout` on a specific read). This turns an invisible, silent failure mode into a greppable, debuggable event without changing any runtime behavior, adding any dependency, or altering the SSE reconnect contract the docstring promises.
