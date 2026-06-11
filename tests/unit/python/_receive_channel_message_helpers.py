"""Shared fixtures for the ``ReceiveChannelMessage`` handler suites.

Extracted from :mod:`test_receive_channel_message` so the producer plan
OQ 5 seeding suite (:mod:`test_receive_channel_message_close_cause`) can
share the servicer/event builders without pushing the original file past
the 500-line cap enforced by ``scripts/checks/file_size.py --strict``.
The leading underscore prevents pytest from collecting this module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import AgentEvent
from agents.server_servicers import AgentServiceServicer

__all__ = [
    "StubAgent",
    "channel_event",
    "enqueued_event",
    "make_servicer",
]


class StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def make_servicer(
    *,
    agents: dict[str, BaseAgent] | None = None,
    enqueue_accepts: bool = True,
) -> tuple[AgentServiceServicer, MagicMock]:
    """Build a servicer over a mock dispatcher.

    ``enqueue_inbound`` is a *synchronous* method returning a bool
    (accepted / dropped); model it with a plain ``MagicMock`` so tests
    can assert call args and toggle the backpressure outcome.
    """
    if agents is None:
        agents = {"ember-owl": StubAgent(agent_id="ember-owl", config={"model": "test"})}
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.enqueue_inbound = MagicMock(return_value=enqueue_accepts)
    return AgentServiceServicer(agents, dispatcher), dispatcher


def channel_event(**overrides: Any) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, Any] = {
        "message_id": "msg-001",
        "channel_id": "group:general",
        "channel_type": "group",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
        # RFC 0011 PR 4b additions: validator now requires
        # ``respond_policy``. Default to ``always`` so the existing
        # cases keep passing; gate-specific tests live in
        # ``test_response_gate.py``.
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


def enqueued_event(dispatcher: MagicMock) -> AgentEvent:
    """The ``AgentEvent`` the handler passed to ``enqueue_inbound``."""
    dispatcher.enqueue_inbound.assert_called_once()
    return dispatcher.enqueue_inbound.call_args.args[1]
