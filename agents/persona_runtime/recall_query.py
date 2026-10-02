"""The text episodic and notes recall search for on a persona turn (ISSUE-0159).

Recall matches any word of its query, up to
:data:`~agents.memory._fts5_query.MAX_MATCH_PHRASES` phrases. A channel
message's prompt text wraps the message in framing ("Message from X:", the
``<|user_message|>`` delimiters), and a scripted turn (a convener opening,
a chair escalation, a synthesis turn) wraps it in instructions long enough
to use up every phrase, so the meeting topic or the stalled question was
never searched. A message is searched by its own words; every other event
by its prompt text. The facts tier keeps reading the prompt text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..persona_types import EventType

if TYPE_CHECKING:
    from ..persona_types import AgentEvent

__all__ = ["recall_text_for_event"]

#: The events whose payload ``content`` is the message the turn answers.
_MESSAGE_EVENT_TYPES = frozenset({EventType.CHANNEL_MESSAGE, EventType.MENTION})


def recall_text_for_event(event: AgentEvent, formatted: str) -> str:
    """The message's own words, or *formatted* (the event's prompt text)."""
    if event.event_type in _MESSAGE_EVENT_TYPES:
        content = (event.payload or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
    return formatted
