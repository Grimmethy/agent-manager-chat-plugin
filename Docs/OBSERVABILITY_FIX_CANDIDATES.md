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

### AC-3 · Silent exception swallow in poll_forever removes all observability of persistent refresh failures
Strength: Strong
Files: session_agent.py
Snippet:
```
        while not self.stop.wait(interval):
            try:
                self.refresh()
            except Exception:  # never let one bad poll kill the agent; never log content
                pass


```

Problem:
In `poll_forever`, the `except Exception: pass` block (with the comment "never let one bad poll kill the agent; never log content") swallows every exception raised by `self.refresh()` with zero diagnostic output. The resilience intent is sound, but the "never log content" constraint is being applied as a blanket prohibition on *any* log line. If `refresh()` begins failing persistently—due to a network partition, an upstream schema change, or a credentials rotation—the agent loop keeps ticking forever, `self.rows` and `self.metas` go stale, events stop being published to `self.hub`, and no log line, warning, or stderr write is ever produced. An operator inspecting the process sees a healthy, running agent with no indication that its data has been frozen since the first failed poll.

Solution:
Replace the bare `pass` with a single `logging.getLogger(__name__).warning("poll_forever: refresh() raised an exception")` call. This records *that* a failure occurred (and, because the loop repeats every `interval`, produces a repeating warning that is trivially greppable and alertable) without logging the exception message, traceback, or any data that `refresh()` might have touched—honouring the "never log content" constraint stated in the existing comment. The `import logging` should be hoisted to the module top-level alongside any other imports rather than placed inside the `except` block. No rethrow is added; the loop continues exactly as before, preserving the "never let one bad poll kill the agent" guarantee. No new dependency is introduced; the stdlib `logging` module is already the project's Python logging primitive.

Benefits:
Operators gain a minimal, content-free signal that `refresh()` is failing, turning an invisible, indefinite stale-data condition into a visible, repeating warning that can be correlated with deployment changes, network incidents, or upstream API breakage. The warning is low-noise (one line per failed poll, no traceback, no exception payload) so it does not flood logs or leak sensitive data, while still being sufficient to trigger an alert rule or a `grep` in a log aggregator. The resilience guarantee is unchanged: a single or even a sustained burst of failures still does not terminate the agent.

### AC-4 · showContextLog swallows non-404 errors as "no entries yet"
Strength: Strong
Files: static/chat.js
Snippet:
```
      const url = `/api/second-brain/file?path=${encodeURIComponent(this.contextLogNotePath())}`;
      const body = await fetchJson(url);
      this.logContent.textContent = body.content || '(empty)';
    } catch (e) {
      // A 404 here just means nothing has been swept into this session's note yet -- a
      // normal, expected state for a short or brand-new conversation, not an error worth
      // alarming over.
```

Problem:
In `showContextLog()`, the `catch (e)` block catches every exception thrown by `fetchJson(url)`—a 404, a 500, a network timeout, a DNS failure, a JSON-parse error—and funnels all of them into the same user-facing string `'No context log entries yet for this session.'`. The comment above the assignment documents only the 404 case ("A 404 here just means…"), but the code never inspects `e` to confirm it is in fact a 404. A user whose second-brain service is returning a 500 or whose network is down sees a calm, reassuring "no entries yet" message and has no reason to suspect anything is wrong. Additionally, the exception object `e` is bound and then discarded: there is no `console.error`, no `console.warn`, no diagnostic trace of any kind, so a developer debugging "why is the context log blank?" must open DevTools → Network and guess.

Solution:
Inside the existing `catch (e)` block, first check whether the error represents the expected 404 (e.g. `e && e.status === 404`, or a string match on `e.message` if `fetchJson` does not attach a `.status` property). For that benign case, keep the current `'No context log entries yet for this session.'` message and `return`. For every other error, call `console.error('showContextLog: failed to load context log', e)` to preserve the original exception for debugging, then set `this.logContent.textContent` to a message that signals a real failure, such as `'Failed to load context log. Please try again.'`. No new dependency, no metrics, no rethrow (the caller is a UI event handler; the `console.error` already surfaces the error to the console).

Benefits:
A 500, network timeout, or parse failure is now visually and textually distinct from the expected empty state, so the user is not told "everything is fine" when the service is actually broken. The `console.error` call gives a developer an immediate, searchable log line carrying the full exception object, eliminating the need to guess from the Network tab. The 404 path is unchanged, so the common "short session, no note yet" case still displays the same friendly message with zero UX regression.

### AC-5 · Silently swallowed OSError on statusline snapshot write
Strength: Strong
Files: statusline_writer.py
Snippet:
```
    directory = (argv[0] if argv else None) or os.environ.get("AM_STATUSLINE_DIR") or DEFAULT_DIR
    try:
        payload = json.loads((stdin or sys.stdin).read(65536))
    except ValueError:
        payload = None
    try:
        write_snapshot(directory, extract(payload))
```

Problem:
In `main`, the call `write_snapshot(directory, extract(payload))` is wrapped in `try / except OSError: pass`. If the target directory (sourced from argv, `AM_STATUSLINE_DIR`, or `DEFAULT_DIR`) is missing, read-only, or over quota, the `OSError` is discarded with a bare `pass`. The function then falls through to `print(status_text(payload))` and `return 0`, so the caller sees a successful exit, nothing reaches `stderr` or any log sink, and the on-disk snapshot is either stale or absent. Any downstream consumer that reads that snapshot file will silently operate on outdated data with no operator-visible signal that the write failed.

Solution:
Replace the bare `except OSError: pass` with `except OSError as exc:` and emit a single `logging.getLogger(__name__).warning("statusline snapshot write to %r failed: %s", directory, exc)` before falling through to the existing `print(status_text(payload))` and `return 0`. Add `import logging` at the top of the module if it is not already present. This keeps the intended "best-effort persistence, still print the status text" contract intact while making the failure visible to any operator who has a log sink or `stderr` attached. No new dependency is introduced; `logging` is stdlib and is already the project's Python logging primitive.

Benefits:
An operator (or CI log collector, cron mail spool, or agent-manager shell that captures `stderr`) will see a single, context-rich warning line naming the exact directory and the OS-level reason the write failed, instead of a silent `0` exit with a stale snapshot. The statusline display path is unaffected—`print(status_text(payload))` still executes unconditionally—so the primary user-facing output is never blocked by a persistence hiccup. The fix is a two-line change (plus the import) and introduces no new dependency or behavioral contract beyond surfacing an error that was previously invisible.

### AC-6 · Silent LookupError swallow in template-save route
Strength: Strong
Files: routes/chat.py
Snippet:
```
    body = request.get_json(silent=True) or {}
    try:
        entry, index = int(body["entry"]), int(body["index"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="entry and index are required integers")
    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session or session.get("status") != "active":
```

Problem:
In the provided source (routes/chat.py, lines 323-342), the `except LookupError:` clause on line 342 carries no body — the source ends at the colon. The `try` block calls `template_store.save_proposal(session, entry, index, body.get("key"), body.get("text"))`, and when that call raises `LookupError` (the natural signal for an invalid `entry` or `index` pointing at a non-existent transcript entry or proposal), the handler discards the exception with no log, no re-raise, and no response. Control falls through to the end of `api_chat_template_save`, which returns `None`. Flask will attempt to serialise `None` as a response body, producing either an empty 200 or a 500 with no diagnostic context. The UI Save button receives no indication of why the save failed, and the server log records nothing, so the failure is invisible to both the user and operators.

Solution:
Give the `except LookupError:` clause a body that (1) logs the failure with enough context to identify the offending request — `session_id`, `entry`, `index`, and the exception message — using the stdlib `logging` module already available in this project, and (2) calls Flask's `abort(404, description=...)` so the caller receives a meaningful HTTP error instead of a silent `None` return. Concretely, replace the bare `except LookupError:` with:

```python
    except LookupError as e:
        logging.error(
            "save_proposal failed: session=%s entry=%s index=%s: %s",
            session_id, entry, index, e,
        )
        abort(404, description="Proposal not found for the given entry/index")
```

This uses only primitives already present in the project (stdlib `logging`, Flask `abort`) and requires no new dependency.

Benefits:
Operators can now see, in the server log, exactly which session, transcript entry, and proposal index triggered the lookup failure and why, turning an invisible failure into a traceable event. The UI Save button receives a 404 with a human-readable description, so the user learns the save did not happen and why, rather than seeing a blank or error-free response. Both the "silent 500 / empty 200" failure mode and the "no log line at all" observability gap are closed with a two-line change.
