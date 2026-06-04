"""Shared input builders for the conversation-window test suites.

Extracted so the parent ``test_conversation_window.py`` and the
deep-review follow-up file ``test_conversation_window_followups.py`` can
both build conversation-window inputs without either file blowing past
the 500-line review cap. The pattern mirrors ``_catchup_test_helpers.py``
— a private module sibling to the test files, imported by name
(``from ._conversation_window_test_helpers import _build``).

Holds builders only — no pytest fixtures. The module-level
``_WINDOW_CACHE`` reset is a small autouse fixture each test file keeps
locally: it is seven lines, and importing an autouse fixture would read
to ruff as an unused import.
"""

from __future__ import annotations

from typing import Any

from agents.persona_runtime.conversation_window import (
    ConversationWindowConfig,
    build_conversation_messages,
)
from agents.persona_types import AgentEvent, EventType

__all__ = [
    "_AGENT_ID",
    "_CHANNEL",
    "_CURRENT",
    "_FakeChannelHistoryFetcher",
    "_build",
    "_event",
    "_row",
]

_AGENT_ID = "ember-owl"
_CHANNEL = "dm:user:ember-owl"
_CURRENT = "<<current event turn>>"


def _row(message_id: str, sender_id: str, content: str) -> dict[str, Any]:
    """One channel-history row in the shape the history endpoint returns."""
    return {"id": message_id, "sender_id": sender_id, "content": content}


def _event(
    *,
    channel_id: str | None = _CHANNEL,
    message_id: str | None = "m-current",
    session_id: str | None = None,
) -> AgentEvent:
    metadata: dict[str, Any] = {}
    if session_id is not None:
        metadata["persatrix_session_id"] = session_id
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "current message body"},
        channel_id=channel_id,
        sender_id="user",
        message_id=message_id,
        metadata=metadata,
    )


class _FakeChannelHistoryFetcher:
    """Duck-typed :class:`ChannelHistoryFetcher` — the seam PR 2 injects
    through. Records calls; returns a fixed result, a per-call sequence,
    or raises."""

    def __init__(
        self,
        result: list[dict[str, Any]] | None = None,
        *,
        results: list[list[dict[str, Any]] | None] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, int]] = []
        self._result = result
        self._results = results
        self._raises = raises

    async def fetch(
        self, channel_id: str, *, limit: int,
    ) -> list[dict[str, Any]] | None:
        self.calls.append((channel_id, limit))
        if self._raises is not None:
            raise self._raises
        if self._results is not None:
            return self._results.pop(0)
        return self._result


async def _build(
    fetcher: _FakeChannelHistoryFetcher,
    *,
    event: AgentEvent | None = None,
    config: ConversationWindowConfig | None = None,
    agent_id: str = _AGENT_ID,
) -> list[dict[str, Any]]:
    return await build_conversation_messages(
        event=event or _event(),
        agent_id=agent_id,
        history_fetcher=fetcher,
        current_user_message=_CURRENT,
        config=config or ConversationWindowConfig(),
    )
