"""Task-local binding of the acting channel's id (ISSUE-0158).

The RFC 0037 §F recall filter (``recall_channel_messages``) needs the room
the persona is **acting in** to apply the audience condition the §D
injection gate applies — the acting room must add nobody the message's
room did not hold. The tool runs deep inside the action loop with no
handle on the originating event, exactly the situation
:mod:`agents.acting_classification` solves for the acting level, so this
module is that seam's twin: one task-local :class:`~contextvars.ContextVar`
bound for the lifetime of an event handler from the trusted
``AgentEvent.channel_id``, never from an LLM argument.

An unbound context reads as ``None``: a tick, a task-agent hop, or any
pre-0158 caller recalls exactly as before (the endpoint applies no
audience condition without an acting channel), which is the additive
contract every scope axis here keeps.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["acting_channel_scope", "current_acting_channel_id"]

_ACTIVE_ACTING_CHANNEL: ContextVar[str | None] = ContextVar(
    "active_acting_channel", default=None,
)


def current_acting_channel_id() -> str | None:
    """The canonical id of the channel bound for this event, or ``None``."""
    return _ACTIVE_ACTING_CHANNEL.get()


@contextmanager
def acting_channel_scope(channel_id: str | None) -> Iterator[None]:
    """Bind *channel_id* for the block; a blank or ``None`` id binds nothing.

    Restores the previous value on exit, including on exception, so nested
    handlers (an executor hop inside a turn) see their own room and hand the
    outer one back.
    """
    if not isinstance(channel_id, str) or not channel_id.strip():
        yield
        return
    token = _ACTIVE_ACTING_CHANNEL.set(channel_id)
    try:
        yield
    finally:
        _ACTIVE_ACTING_CHANNEL.reset(token)
