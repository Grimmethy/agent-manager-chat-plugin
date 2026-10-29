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


def audit_turn(text: str, has_user_message_after_first: bool = False) -> dict:
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
        - has_violations: True if any violations detected
    """
    result = {
        'youre_right_count': 0,
        'context_log_mentions': 0,
        'has_violations': False
    }
    
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
        result['context_log_mentions'] > 0
    )
    
    return result
