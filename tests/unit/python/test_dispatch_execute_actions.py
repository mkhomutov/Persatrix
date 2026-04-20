"""
Tests for EventDispatcher.dispatch(execute_actions=False/True).

Verifies that the flag controls whether ActionExecutor.execute() is called,
without changing the returned action list. (RFC 0016 PR 3, OQ 7)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import EventDispatcher
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.tools.registry import clear_registry


# ─── Fixtures ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_mock_agent(actions: list[AgentAction]) -> MagicMock:
    """Return a mock _LLMPersonaAgent that yields *actions* from on_event()."""
    agent = MagicMock()
    agent.on_event = AsyncMock(return_value=actions)
    return agent


def _make_dispatcher(
    agent_id: str,
    actions: list[AgentAction],
) -> tuple[EventDispatcher, MagicMock]:
    """Create a dispatcher pre-loaded with one mock agent."""
    agent = _make_mock_agent(actions)
    dispatcher = EventDispatcher(agents={agent_id: agent})
    return dispatcher, agent


def _chat_event() -> AgentEvent:
    return AgentEvent(
        event_type=EventType.MESSAGE_RECEIVED,
        payload={"content": "hello"},
    )


# ─── execute_actions=False ────────────────────────────────────


class TestExecuteActionsFlag:

    async def test_execute_actions_false_returns_actions_without_executing(self):
        """dispatch(execute_actions=False) returns actions but does not call executor."""
        actions = [AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": ["local"]})]
        dispatcher, _ = _make_dispatcher("ember-owl", actions)

        # Patch executor.execute so we can assert it was NOT called.
        executor_mock = AsyncMock(return_value=[])
        dispatcher._executor.execute = executor_mock

        result = await dispatcher.dispatch("ember-owl", _chat_event(), execute_actions=False)

        assert result == actions
        executor_mock.assert_not_called()

    async def test_execute_actions_true_calls_executor(self):
        """dispatch(execute_actions=True) (default) still calls executor."""
        actions = [AgentAction(ActionType.DO_NOTHING, {})]
        dispatcher, _ = _make_dispatcher("ember-owl", actions)

        executor_mock = AsyncMock(return_value=[{"action_type": "do_nothing", "status": "ok"}])
        dispatcher._executor.execute = executor_mock

        result = await dispatcher.dispatch("ember-owl", _chat_event(), execute_actions=True)

        assert result == actions
        executor_mock.assert_called_once()

    async def test_execute_actions_default_is_true(self):
        """Omitting execute_actions behaves like execute_actions=True."""
        actions = [AgentAction(ActionType.COMPLETE_TASK, {"result": "done"})]
        dispatcher, _ = _make_dispatcher("ember-owl", actions)

        executor_mock = AsyncMock(return_value=[])
        dispatcher._executor.execute = executor_mock

        await dispatcher.dispatch("ember-owl", _chat_event())

        executor_mock.assert_called_once()

    async def test_execute_actions_false_child_dispatches_still_use_default_true(self):
        """execute_actions=False is a per-call override; child dispatches default to True."""
        # A SEND_MESSAGE action from agent A would trigger a child dispatch to B
        # via ActionExecutor._handle_send_message().  That child dispatch should
        # call agent B's on_event AND execute actions normally.
        #
        # This test verifies that execute_actions=False does NOT propagate to
        # child dispatches — the flag is purely per-call. (OQ 7)
        agent_b = _make_mock_agent([AgentAction(ActionType.DO_NOTHING, {})])
        agent_a = _make_mock_agent([
            AgentAction(ActionType.SEND_MESSAGE, {"content": "hi", "mentions": ["iron-fox"]}),
        ])

        dispatcher = EventDispatcher(agents={"ember-owl": agent_a, "iron-fox": agent_b})

        # With execute_actions=False, executor is NOT called at the top level,
        # so the SEND_MESSAGE is never routed — agent_b.on_event stays uncalled.
        executor_mock = AsyncMock(return_value=[])
        dispatcher._executor.execute = executor_mock

        result = await dispatcher.dispatch(
            "ember-owl", _chat_event(), execute_actions=False,
        )

        # Action list returned unchanged
        assert len(result) == 1
        assert result[0].action_type == ActionType.SEND_MESSAGE
        # Executor was NOT called (no routing happened)
        executor_mock.assert_not_called()
        # agent_b never received anything
        agent_b.on_event.assert_not_called()

    async def test_unknown_agent_returns_empty_list(self):
        """dispatch() to an unknown agent returns [] regardless of execute_actions."""
        dispatcher = EventDispatcher(agents={})
        result = await dispatcher.dispatch(
            "ghost", _chat_event(), execute_actions=False,
        )
        assert result == []

    async def test_cascade_depth_limit_respected_with_flag(self):
        """Cascade depth limit still drops events when execute_actions=False."""
        actions = [AgentAction(ActionType.DO_NOTHING, {})]
        dispatcher, agent = _make_dispatcher("ember-owl", actions)

        deep_event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "deep"},
            metadata={"cascade_depth": 10},  # beyond default 5
        )

        result = await dispatcher.dispatch(
            "ember-owl", deep_event, execute_actions=False,
        )
        assert result == []
        agent.on_event.assert_not_called()
