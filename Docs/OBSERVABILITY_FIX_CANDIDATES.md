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

### AC-2 · SSE relay silently swallows upstream stream errors
Strength: Strong
Files: routes/chat.py
Snippet:
```
    def relay():
        try:
            yield from agent_client.filter_events(agent_client.iter_chunks(upstream))
        except (requests.RequestException, OSError):
            pass
        finally:
            upstream.close()
```

Problem:
Inside the `relay()` generator in routes/chat.py, the `except (requests.RequestException, OSError): pass` clause discards the exception object entirely. The docstring confirms the intent is to let the stream end so the browser's EventSource can reconnect, and the `finally: upstream.close()` releases the socket, so the control flow is correct. However, the exception itself is never referenced, logged, or recorded anywhere. If the upstream agent is persistently unreachable or the network path is broken, every SSE stream will silently terminate, the browser will silently reconnect, and the next stream will silently terminate again — producing zero server-side log entries. With no metrics system in this project, the stdlib `logging` module is the only available observability channel, and this code path uses none of it. An operator investigating a broken agent at 3 a.m. would see nothing in the logs.

Solution:
Add `import logging` at the top of routes/chat.py (alongside the existing imports) and a module-level `logger = logging.getLogger(__name__)`. Inside the `relay()` generator, change the bare `except (requests.RequestException, OSError): pass` to `except (requests.RequestException, OSError) as exc:` followed by a single `logger.warning("SSE relay: upstream agent stream ended with error: %s", exc, exc_info=True)`. The `finally: upstream.close()` line is unchanged. No re-raise (would break the SSE contract by turning a clean stream-end into a 500 mid-body), no new dependency, no metric emission.

Benefits:
A persistent upstream failure now produces a repeating, timestamped, greppable `WARNING` line in the server log with the exception type, message, and full traceback (via `exc_info=True`). An operator can immediately distinguish "agent is down" from "client disconnected" and see the root-cause exception. The fix is a single log statement; control flow, the SSE contract, and resource cleanup are all unchanged.
