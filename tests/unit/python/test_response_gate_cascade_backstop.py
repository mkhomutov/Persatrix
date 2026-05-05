"""RFC 0011 PR 4b — defense-in-depth ordering test.

The response gate is the **primary** drop point for non-mentioning
``CHANNEL_MESSAGE`` traffic, but it is not the only one. The
:class:`agents.dispatch.EventDispatcher` enforces a global
``max_cascade_depth`` ceiling that fires *before* any agent-side code
(it is the first check in :meth:`EventDispatcher.dispatch`), so a
runaway cascade between two ``always``-respond agents cannot escape the
backstop even if the gate would have admitted every link in the chain.

The contract this file pins:

* An event arriving with ``cascade_depth >= max_cascade_depth`` is
  dropped by the dispatcher.
* The agent's ``on_event`` is **never invoked**, so the gate's
  ``channel.messages.gated`` counter does not fire either — the drop
  is attributable to the depth backstop, not the policy gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agents.dispatch import EventDispatcher
from agents.persona_types import AgentEvent, EventType


def _stub_agent_with_on_event() -> MagicMock:
    """Build a stand-in agent whose ``on_event`` is observable.

    The dispatcher only invokes ``on_event`` after the cascade-depth
    check passes; using a ``MagicMock`` here lets us assert the call
    count without booting the real LLM persona runtime.
    """
    agent = MagicMock()
    agent.on_event = AsyncMock(return_value=[])
    return agent


class TestCascadeBackstop:
    async def test_event_at_depth_limit_is_dropped_before_on_event(self):
        """An event arriving at the depth ceiling never reaches the agent.

        This is the global backstop that protects against
        ``always``-respond pairs cascading without bound.
        """
        agent = _stub_agent_with_on_event()
        dispatcher = EventDispatcher(
            agents={"agent-a": agent}, max_cascade_depth=5,
        )

        # depth=5 is at the ceiling — the dispatcher's first check fires
        # before any agent-side gate runs.
        evt = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            channel_id="group:planning",
            sender_id="agent-b",
            payload={
                "content": "loop?",
                "respond_policy": "always",  # gate would have admitted
                "mentions": ["agent-a"],
            },
            metadata={"cascade_depth": 5},
        )
        actions = await dispatcher.dispatch("agent-a", evt)

        assert actions == [], "depth-ceiling events MUST yield no actions"
        agent.on_event.assert_not_called()

    async def test_event_just_below_depth_limit_reaches_agent(self):
        """Negative case: depth-1-below-limit events still reach the agent.

        Without this guard, a regression that off-by-ones the comparison
        could silently strand legitimate traffic.
        """
        agent = _stub_agent_with_on_event()
        dispatcher = EventDispatcher(
            agents={"agent-a": agent}, max_cascade_depth=5,
        )
        evt = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            channel_id="group:planning",
            sender_id="agent-b",
            payload={
                "content": "ok",
                "respond_policy": "always",
                "mentions": ["agent-a"],
            },
            metadata={"cascade_depth": 4},
        )
        await dispatcher.dispatch("agent-a", evt)
        agent.on_event.assert_called_once()
