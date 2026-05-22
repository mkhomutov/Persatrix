"""No-running-loop inbound fallback backpressure (RFC 0024 Phase 4 follow-up).

Split from ``test_event_dispatcher.py`` to keep both files under the
project's 500-line code-file cap.

``EventDispatcher.enqueue_inbound`` routes a channel message through the
agent's per-agent ``EventLoop`` when one is running (bounded by the loop's
queue, discard-not-block). But a **reactive** persona has no
``TickScheduler`` registered (``agents/server_persona.py`` only starts one
for semi-autonomous / autonomous agents), so its channel messages take the
no-loop fallback: a detached task scheduled into ``_inbound_fallback_tasks``.
Without a cap, a chatty or abusive producer on the cleartext gRPC port grows
that set without bound — the slow-burn DoS surface the now-removed servicer
``_MAX_PENDING_DISPATCHES`` cap (PR #248 deep review **Low**) defended
against, unconditionally, for every channel dispatch.

The cap restores that invariant on the path the EventLoop queue does not
cover: once full, ``enqueue_inbound`` returns ``False`` (→
``TaskAck(success=False)``), symmetric with the queue-full path.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import EventDispatcher
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


class TestEnqueueInboundNoLoopBackpressure:

    @staticmethod
    def _mock_agent(agent_id: str = "ember-owl") -> MagicMock:
        agent = MagicMock()
        agent.agent_id = agent_id
        agent.on_event = AsyncMock(return_value=[])
        return agent

    @staticmethod
    def _event() -> AgentEvent:
        return AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},
            channel_id="group:general",
            sender_id="iron-fox",
            metadata={"cascade_depth": 0},
        )

    async def test_no_loop_fallback_rejects_when_full(self, monkeypatch):
        """A full ``_inbound_fallback_tasks`` set rejects (returns ``False``)
        without scheduling a new task or invoking the agent."""
        import agents.dispatch as dispatch_mod

        monkeypatch.setattr(dispatch_mod, "_MAX_INBOUND_FALLBACK_TASKS", 2)

        agent = self._mock_agent()
        # No tick scheduler registered → no-loop fallback path.
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        gate = asyncio.Event()

        async def _parked() -> None:
            await gate.wait()

        parked = [asyncio.create_task(_parked()) for _ in range(2)]
        dispatcher._inbound_fallback_tasks.update(parked)

        try:
            accepted = dispatcher.enqueue_inbound("ember-owl", self._event())
            assert accepted is False
            # No new task was scheduled and the agent was never run.
            assert len(dispatcher._inbound_fallback_tasks) == 2
            agent.on_event.assert_not_called()
        finally:
            gate.set()
            await asyncio.gather(*parked)

    async def test_no_loop_fallback_accepts_below_cap_and_runs_agent(
        self, monkeypatch,
    ):
        """Below the cap the fallback accepts, schedules a detached task,
        and that task actually processes the event (runs ``on_event``)."""
        import agents.dispatch as dispatch_mod

        monkeypatch.setattr(dispatch_mod, "_MAX_INBOUND_FALLBACK_TASKS", 2)

        agent = self._mock_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        accepted = dispatcher.enqueue_inbound("ember-owl", self._event())
        assert accepted is True

        # Drain the scheduled fallback task; the done-callback discards it.
        await asyncio.gather(*list(dispatcher._inbound_fallback_tasks))
        agent.on_event.assert_awaited_once()
        assert len(dispatcher._inbound_fallback_tasks) == 0

    async def test_no_loop_fallback_unknown_agent_returns_false(self):
        """An unknown target is rejected before any task is scheduled."""
        dispatcher = EventDispatcher(agents={})
        accepted = dispatcher.enqueue_inbound("nope", self._event())
        assert accepted is False
        assert len(dispatcher._inbound_fallback_tasks) == 0
