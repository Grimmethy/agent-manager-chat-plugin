# Performance Fix Candidates

### AC-1 · Blocking I/O per Host in Loop
Strength: Strong
Files: agent_client.py
Snippet:
```
    """
    sessions = []
    skipped = []
    for host in agent_hosts.hosts():
        headers = {}
        token = agent_hosts.read_token(host)
        if token:
```

Problem:
The loop iterates over `agent_hosts.hosts()`, which yields distinct hosts. For each host, `agent_hosts.read_token(host)` is called — a blocking I/O operation (disk or network) — meaning each iteration must wait for the result before proceeding. Even for small host lists, this creates a sequential bottleneck that scales poorly with host count or slow token-fetching backends. The scanner’s “blocking-call-in-loop” rule correctly flags this as a performance risk, and the grounding source confirms the call is inside the loop with no opportunity for caching or batching unless the loop is restructured.

Solution:
Pre-fetch all tokens in parallel using `ThreadPoolExecutor`, storing results in a dictionary keyed by host. Then, iterate over the original host list and look up tokens from the pre-fetched map. This avoids the blocking I/O per loop iteration by batching the I/O operations. The fix uses only stdlib primitives — no async, no metrics, no logging framework — and matches the project’s existing capabilities.

Benefits:
The fix eliminates the per-iteration blocking I/O, reducing total execution time for large or slow token-fetching scenarios. It preserves loop order and avoids state changes, while making the code more scalable without introducing new dependencies or complexity.
