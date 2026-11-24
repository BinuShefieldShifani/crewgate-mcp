"""Write tools run only when the user asked for that kind of change.

The gateway already makes every write wait for a person's approval, so a model fooled by text in
tool data can at most create a request nobody wanted. This is the layer before that: the agent
doesn't even send the request unless the user's latest message asks to book (or swap), or answers
yes to the agent asking. Found by the evals: with instructions hidden in an airline's name,
gpt-oss-20b proposed a booking the user never asked for; gpt-oss-120b didn't.
"""

import re
from collections.abc import Sequence

from crewgate.plugins.booking.confirmation import plain

from .llm import Message

_ASKS = {
    "booking_book_flight": re.compile(r"\b(book|reserve|purchase|buy|boka|bokning|köp)\w*", re.IGNORECASE),
    "crew_propose_swap": re.compile(
        r"\b(swap|switch|hand(ing)? over|reassign|giv(e|ing)|take over|propos|byt|lämna över)\w*",
        re.IGNORECASE,
    ),
}
_YES = re.compile(
    r"^\s*(yes|yep|yeah|sure|ok|okay|go ahead|please do|do it|confirm|ja|absolut|kör|gör det)\b",
    re.IGNORECASE,
)
NOT_REQUESTED = {
    "error": "not_requested",
    "message": (
        "The user has not asked for this change in their latest message. Ask them first; do not retry."
    ),
}


def asked_for(tool: str, messages: Sequence[Message]) -> bool:
    """True for read tools; for a write tool, whether the latest user turn asks for it."""
    pattern = _ASKS.get(tool)
    if pattern is None:
        return True
    user_at = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
    if user_at is None:
        return False
    text = plain(str(messages[user_at].get("content") or ""))
    if pattern.search(text):
        return True
    if _YES.search(text):  # "yes" to the agent's own "shall I book it?"
        before = next(
            (m for m in reversed(messages[:user_at]) if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        return before is not None and bool(pattern.search(plain(str(before["content"]))))
    return False
