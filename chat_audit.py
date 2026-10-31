"""Deterministic post-turn audit for chat behavioral violations.

Counts model-generated phrases that indicate the assistant is:
- Inventing user reactions ("You're right", "You're absolutely right")
- Narrating internal protocol (mentioning CONTEXT-LOG outside the footer block)
- Repeating filler between tool calls

This is a model-behavior audit: it counts what the model said, not whether
the answer was correct. The behavioral guidance rule in chat_sessions.py
prevents these violations; this module measures compliance.

The audit result is stored in the turn record's "audit" field for tracking.
"""
import re

# Pattern matching "You're right" or "You're absolutely right" at start of line
_YOURE_RIGHT_PATTERN = re.compile(
    r"^\s*you'?re\s+(absolutely\s+)?right\b",
    re.IGNORECASE
)

# Pattern to find the CONTEXT-LOG footer block (last one in the text)
_CONTEXT_LOG_FOOTER_PATTERN = re.compile(
    r"\n\s*CONTEXT-LOG:\s*\S+",
    re.MULTILINE
)


# Tools that change state (core's chat write tools: src/local-tool-client.js buildChatToolHandlers). run_bash counts only when the call started a
# background process (tool_log marks it `background`). Keep this list in step with core when a chat write tool is added.
WRITE_TOOLS = frozenset({
    'record_branch_verdict', 'queue_reviewed_task', 'requeue_blocked_task', 'withdraw_task',
    'answer_task_clarification', 'resolve_task_clarification', 'mark_task_clarification_done',
})

# A prohibition in the USER's message. Each maps to the write tools it forbids; 'only report' forbids every write tool.
_PROHIBITIONS = (
    (re.compile(r"\b(?:do\s*not|don'?t)\s+record\b|\bno\s+recording\b|\bwithout\s+recording\b", re.I), frozenset({'record_branch_verdict'})),
    (re.compile(r"\b(?:do\s*not|don'?t)\s+file\b|\bno\s+filing\b|\bwithout\s+filing\b", re.I), frozenset({'queue_reviewed_task'})),
    (re.compile(r"\bonly\s+report\b", re.I), WRITE_TOOLS),
)
# Words in the user's message that make a queue_reviewed_task / withdraw_task call a requested one.
_TASK_WORDS = re.compile(r"\b(?:file|filed|filing|queue|queued|task|tasks|requeue|withdraw|retire)\b", re.I)
_BACKGROUND_WORDS = re.compile(r"\b(?:background|nohup|in\s+the\s+background)\b", re.I)


def unrequested_writes(user_message: str, tools) -> list:
    """Names of the tool calls in `tools` ({name, ...} entries as tool_log stores them) that the USER's message did not ask for.

    (a) the message prohibits recording / filing / anything ('only report') and a write tool of that kind ran;
    (b) queue_reviewed_task or withdraw_task ran and the message has none of file, queue, task, requeue, withdraw, retire;
    (c) run_bash started a background process and the message never mentions background or nohup.
    Pure; reads the user's words, never the assistant's reply."""
    message = user_message if isinstance(user_message, str) else ''
    forbidden = set()
    for pattern, names in _PROHIBITIONS:
        if pattern.search(message):
            forbidden |= names
    asked_for_tasks = bool(_TASK_WORDS.search(message))
    asked_for_background = bool(_BACKGROUND_WORDS.search(message))
    flagged = []
    for call in tools or []:
        if not isinstance(call, dict):
            continue
        name = call.get('name')
        if name in forbidden:
            flagged.append(name)
        elif name in ('queue_reviewed_task', 'withdraw_task') and not asked_for_tasks:
            flagged.append(name)
        elif name == 'run_bash' and call.get('background') and not asked_for_background:
            flagged.append(name)
    return flagged


def audit_turn(text: str, has_user_message_after_first: bool = False, user_message: str = None, tools=None) -> dict:
    """Audit a turn's text for behavioral violations.
    
    Args:
        text: The assistant's response text (after streaming)
        has_user_message_after_first: True if this turn has a user message 
            following the first one (multi-turn exchange). When False (single
            turn), count "You're right" phrases as violations since the user
            couldn't have said them.
    
    Returns:
        Dict with audit results:
        - youre_right_count: Number of "You're right" phrases (counted only when
          has_user_message_after_first=False)
        - context_log_mentions: Number of CONTEXT-LOG mentions outside the footer
          block (footers are OK, mentions in body are violations)
        - unrequestedWrites / unrequestedWriteTools: write tool calls the user's
          message did not ask for (see unrequested_writes); 0 and [] when the
          caller passes no user_message/tools (Claude turns, old callers)
        - has_violations: True if any violations detected
    """
    result = {
        'youre_right_count': 0,
        'context_log_mentions': 0,
        'unrequestedWrites': 0,
        'unrequestedWriteTools': [],
        'has_violations': False
    }
    if user_message is not None or tools:
        result['unrequestedWriteTools'] = unrequested_writes(user_message, tools)
        result['unrequestedWrites'] = len(result['unrequestedWriteTools'])
    
    # Count "You're right" phrases - only when no user message follows
    if not has_user_message_after_first:
        lines = text.split('\n')
        for line in lines:
            if _YOURE_RIGHT_PATTERN.match(line):
                result['youre_right_count'] += 1
    
    # Count CONTEXT-LOG mentions outside the footer block
    # Find the last CONTEXT-LOG footer (the actual footer block)
    footer_match = None
    for match in _CONTEXT_LOG_FOOTER_PATTERN.finditer(text):
        footer_match = match  # Keep the last match
    
    if footer_match:
        # Only count CONTEXT-LOG mentions before the footer
        footer_start = footer_match.start()
        before_footer = text[:footer_start]
    else:
        # No footer found, count all CONTEXT-LOG mentions
        before_footer = text
    
    # Count CONTEXT-LOG mentions in the body (not the footer itself)
    context_log_pattern = re.compile(r'CONTEXT-LOG', re.IGNORECASE)
    result['context_log_mentions'] = len(context_log_pattern.findall(before_footer))
    
    # Determine if there are any violations
    result['has_violations'] = (
        result['youre_right_count'] > 0 or
        result['context_log_mentions'] > 0 or
        result['unrequestedWrites'] > 0
    )
    
    return result
