"""Inbound cascade-depth threading for ``TickScheduler`` — RFC 0024 PR 5.1.

Split out of ``test_tick_scheduler.py`` to keep that file under the
500-line review-friendly cap.

The fire-and-forget inbound (channel) path must honour the dispatcher's
*configured* ``max_cascade_depth``, not a hardcoded default (PR 4
review (1)). Before the fix, ``TickScheduler._handle_inbound_event``
passed ``DEFAULT_MAX_CASCADE_DEPTH`` literally, so a deployment that
constructed ``EventDispatcher(max_cascade_depth=X)`` had its override
silently ignored on the dominant channel path while ``dispatch()`` and
the no-loop fallback honoured it — two sources of truth.

Uses a lightweight agent stub: ``_handle_inbound_event`` only reads
``agent.agent_id`` (for the ``EventLoop`` ctor) and forwards the agent
to the monkeypatched ``process_inbound_channel_event``, so no real
persona / memory setup is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from agents.dispatch import ActionExecutor, EventDispatcher
from agents.persona_types import AgentEvent, EventType
from agents.tick import TickScheduler


def _channel_event() -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"text": "hi"},
        channel_id="general",
        sender_id="someone",
        message_id="m1",
    )


def _agent_stub(agent_id: str = "ember-owl") -> MagicMock:
    agent = MagicMock()
    agent.agent_id = agent_id
    return agent


class TestInboundCascadeDepth:
    async def test_inbound_uses_dispatcher_configured_cascade_depth(
        self, monkeypatch,
    ):
        captured: dict[str, int] = {}

        async def _fake_process(*, agent, executor, event, max_cascade_depth):
            captured["depth"] = max_cascade_depth

        monkeypatch.setattr(
            "agents.chat_reply.process_inbound_channel_event", _fake_process,
        )

        # Non-default ceiling — distinct from DEFAULT_MAX_CASCADE_DEPTH (5).
        dispatcher = EventDispatcher(max_cascade_depth=2)
        scheduler = TickScheduler(
            _agent_stub(), executor=dispatcher.executor,
            register_legacy_timer=False,
        )

        await scheduler._handle_inbound_event(_channel_event())

        assert captured["depth"] == 2
        assert DEFAULT_MAX_CASCADE_DEPTH != 2  # guard: 2 is genuinely non-default

    async def test_inbound_falls_back_to_default_without_dispatcher(
        self, monkeypatch,
    ):
        """A bare ``ActionExecutor`` (no dispatcher wired) keeps the
        documented default ceiling — the fallback must not crash on the
        ``executor.dispatcher is None`` path used by session-less fixtures."""
        captured: dict[str, int] = {}

        async def _fake_process(*, agent, executor, event, max_cascade_depth):
            captured["depth"] = max_cascade_depth

        monkeypatch.setattr(
            "agents.chat_reply.process_inbound_channel_event", _fake_process,
        )

        scheduler = TickScheduler(
            _agent_stub(), executor=ActionExecutor(),
            register_legacy_timer=False,
        )

        await scheduler._handle_inbound_event(_channel_event())

        assert captured["depth"] == DEFAULT_MAX_CASCADE_DEPTH
