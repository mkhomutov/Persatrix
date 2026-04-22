"""Unit tests for RFC 0017 §F: empty-context TICK short-circuit.

Tests the four-condition guard in _ActionLoopMixin._on_event_inner:
  1. event.event_type == TICK
  2. memory_admitted_tokens == 0
  3. no active goal payload (_has_active_goal_payload() == False)
  4. no pending conversation turn (_has_pending_turn() == False)

When all four hold, _on_event_inner returns [DO_NOTHING] without calling
the LLM.  The TickScheduler's existing all_do_nothing branch then
increments idle_count.

Module pin (RFC 0017 PR plan open-at-plan-time resolution):
  TICK handler:  agents/persona_runtime/action_loop.py
                 _ActionLoopMixin._on_event_inner
  Accessors:     agents/persona_runtime/__init__.py
                 _LLMPersonaAgent._has_active_goal_payload
                 _LLMPersonaAgent._has_pending_turn
"""

from __future__ import annotations

import copy
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.tick import TickScheduler

# ─── Helpers ──────────────────────────────────────────────────────────────────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "test-agent",
    "type": "persona",
    "name": "Test Agent",
    "role": "Testing",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "background": "Test background.",
        "behavior": {},
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}

_DO_NOTHING_RESPONSE = LLMResponse(
    text='[{"action_type": "do_nothing", "payload": {}}]',
    stop_reason=StopReason.END_TURN,
    usage=Usage(input_tokens=10, output_tokens=5),
)

_SUBSTANTIVE_RESPONSE = LLMResponse(
    text='[{"action_type": "complete_task", "payload": {"result": "done"}}]',
    stop_reason=StopReason.END_TURN,
    usage=Usage(input_tokens=10, output_tokens=10),
)


def _make_client(response: LLMResponse = _SUBSTANTIVE_RESPONSE) -> LLMClient:
    """Return a mock LLMClient that returns the given response."""
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(return_value=response)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(return_value=[])
    return LLMClient(mock_provider)


async def _make_agent(
    *,
    goal_progress: dict[str, float] | None = None,
    recent_context: list[str] | None = None,
    client: LLMClient | None = None,
) -> _LLMPersonaAgent:
    """Create an initialized _LLMPersonaAgent with optional state preset."""
    agent = create_persona_agent(
        agent_id="test-agent",
        # Deep-copy so per-test mutations of nested dicts (``persona``,
        # ``permissions``, ``memory``) cannot leak across tests via the
        # shared module-level _PERSONA_CONFIG reference.
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client or _make_client(),
    )
    await agent.initialize_memory()
    if goal_progress is not None:
        agent._state.goal_progress = goal_progress
    if recent_context is not None:
        agent._state.recent_context = recent_context
    return agent


def _zero_injection() -> MemoryInjectionResult:
    """Return a MemoryInjectionResult with zero admitted tokens."""
    return MemoryInjectionResult(memory_admitted_tokens=0)


def _nonzero_injection(tokens: int = 200) -> MemoryInjectionResult:
    """Return a MemoryInjectionResult with non-zero admitted tokens."""
    return MemoryInjectionResult(memory_admitted_tokens=tokens)


# ─── Core short-circuit cases ─────────────────────────────────────────────────


class TestTickShortCircuit:
    """Five required test cases (a)–(e) from RFC 0017 §F test strategy."""

    @pytest.mark.asyncio
    async def test_a_empty_context_tick_suppresses_llm_call(self) -> None:
        """(a) Empty-context TICK with no goal/turn → DO_NOTHING, no LLM call."""
        client = _make_client()
        agent = await _make_agent(client=client)

        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_zero_injection(),
        ):
            actions = await agent.on_event(AgentEvent(event_type=EventType.TICK))

        assert len(actions) == 1
        assert actions[0].action_type == ActionType.DO_NOTHING
        # LLM must NOT have been called (create_message call_count == 0).
        client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_b_nonempty_context_tick_calls_llm(self) -> None:
        """(b) Non-empty TICK (memory admitted) → LLM call still issued."""
        client = _make_client()
        agent = await _make_agent(client=client)

        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_nonzero_injection(tokens=300),
        ):
            actions = await agent.on_event(AgentEvent(event_type=EventType.TICK))

        # LLM MUST have been called.
        client._provider.create_message.assert_called_once()  # type: ignore[attr-defined]
        # The action comes from the LLM response (complete_task, not DO_NOTHING
        # from the short-circuit).
        assert any(
            a.action_type == ActionType.COMPLETE_TASK for a in actions
        ), f"Expected COMPLETE_TASK from LLM, got {[a.action_type for a in actions]}"

    @pytest.mark.asyncio
    async def test_c_empty_context_tick_with_active_goal_calls_llm(self) -> None:
        """(c) Empty-context TICK with active goal payload → LLM call still issued."""
        client = _make_client()
        agent = await _make_agent(
            client=client,
            goal_progress={"ship-v2": 0.4},
        )

        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_zero_injection(),
        ):
            await agent.on_event(AgentEvent(event_type=EventType.TICK))

        # Active goal → guard condition 3 is False → short-circuit must NOT fire.
        # Key assertion is that the LLM was called; the returned action type
        # depends on the mock response and is not asserted here (the prior
        # ``or True`` tautology was removed per PR #149 review M-2).
        client._provider.create_message.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_d_empty_context_tick_with_pending_turn_calls_llm(self) -> None:
        """(d) Empty-context TICK with pending conversation turn → LLM call still issued."""
        client = _make_client()
        agent = await _make_agent(
            client=client,
            recent_context=["User said: hello — please follow up"],
        )

        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_zero_injection(),
        ):
            await agent.on_event(AgentEvent(event_type=EventType.TICK))

        # Pending turn → guard condition 4 is False → short-circuit must NOT fire.
        client._provider.create_message.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_e_non_tick_event_with_zero_memory_calls_llm(self) -> None:
        """(e) Non-TICK event with memory_admitted_tokens == 0 → LLM call still issued.

        The short-circuit must only fire on TICK events.  A low-keyword
        MESSAGE_RECEIVED that admits zero memory tokens must still invoke
        the LLM (the user is waiting for a reply).
        """
        client = _make_client()
        agent = await _make_agent(client=client)

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "hi"},
        )

        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_zero_injection(),
        ):
            await agent.on_event(event)

        # Non-TICK → guard condition 1 is False → short-circuit must NOT fire.
        client._provider.create_message.assert_called_once()  # type: ignore[attr-defined]


# ─── Idle suppression ─────────────────────────────────────────────────────────


def _simulate_do_nothing_tick(scheduler: TickScheduler, actions: list[AgentAction]) -> bool:
    """Mirror ``TickScheduler._run()``'s ``all_do_nothing`` branch in tests.

    Increments the scheduler's idle counter when every action is
    ``DO_NOTHING``, exactly as the production loop does.  Centralising the
    coupling to the private ``_idle_count`` attribute here keeps the
    private-attribute access in one place so ``TickScheduler`` refactors
    only need to update this helper, not every test.
    (PR 6 — RFC 0017 PR 5 review finding 1.)

    Returns the ``all_do_nothing`` bool for callers that want to assert on it.
    """
    all_do_nothing = all(a.action_type == ActionType.DO_NOTHING for a in actions)
    if all_do_nothing:
        scheduler._idle_count += 1  # type: ignore[attr-defined]
    return all_do_nothing


class TestIdleSuppression:
    """idle_count reaches idle_after_ticks with zero LLM calls during suppressed ticks."""

    @pytest.mark.asyncio
    async def test_idle_count_increments_on_suppressed_tick(self) -> None:
        """idle_count advances by 1 per suppressed TICK (via TickScheduler logic)."""
        client = _make_client()
        agent = await _make_agent(client=client)

        scheduler = TickScheduler(agent, interval=60.0, idle_after_ticks=3)

        # Simulate what TickScheduler._run does: call on_tick, count DO_NOTHING.
        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_zero_injection(),
        ):
            actions = await agent.on_tick()

        all_do_nothing = _simulate_do_nothing_tick(scheduler, actions)
        assert all_do_nothing, (
            "Suppressed TICK must return only DO_NOTHING actions so "
            "TickScheduler._run increments idle_count"
        )

        assert scheduler.idle_count == 1
        client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_reaches_idle_after_n_suppressed_ticks(self) -> None:
        """After idle_after_ticks empty-context TICKs, is_idle becomes True; 0 LLM calls."""
        idle_after = 3
        client = _make_client()
        agent = await _make_agent(client=client)

        scheduler = TickScheduler(agent, interval=60.0, idle_after_ticks=idle_after)

        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=_zero_injection(),
        ):
            for _ in range(idle_after):
                actions = await agent.on_tick()
                _simulate_do_nothing_tick(scheduler, actions)

        assert scheduler.is_idle
        # Zero LLM calls across all suppressed ticks.
        client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]


# ─── Debug log emission ───────────────────────────────────────────────────────


class TestDebugLog:
    """DEBUG log entry is emitted with expected extra fields on each suppressed tick."""

    @pytest.mark.asyncio
    async def test_debug_log_emitted_on_suppressed_tick(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Suppressed TICK emits DEBUG log with reason='empty_context_tick'."""
        agent = await _make_agent()

        with (
            patch.object(
                agent,
                "_inject_memory_context",
                return_value=_zero_injection(),
            ),
            caplog.at_level(logging.DEBUG, logger="agents.persona_runtime.action_loop"),
        ):
            await agent.on_event(AgentEvent(event_type=EventType.TICK))

        suppression_logs = [
            r for r in caplog.records
            if getattr(r, "reason", None) == "empty_context_tick"
        ]
        assert len(suppression_logs) == 1, (
            f"Expected exactly 1 suppression log, found {len(suppression_logs)}"
        )
        assert suppression_logs[0].levelno == logging.DEBUG
        # NEW-N-2 (PR #149 re-review): use getattr for symmetry with the
        # filter above — keeps the assertion message clean if `extra`
        # propagation ever changes (otherwise the bare attribute access
        # raises AttributeError instead of producing a clear assert).
        assert getattr(suppression_logs[0], "agent_id", None) == "test-agent"

    @pytest.mark.asyncio
    async def test_debug_log_not_emitted_when_llm_called(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-suppressed tick (non-zero memory) emits no suppression log."""
        agent = await _make_agent()

        with (
            patch.object(
                agent,
                "_inject_memory_context",
                return_value=_nonzero_injection(tokens=150),
            ),
            caplog.at_level(logging.DEBUG, logger="agents.persona_runtime.action_loop"),
        ):
            await agent.on_event(AgentEvent(event_type=EventType.TICK))

        suppression_logs = [
            r for r in caplog.records
            if getattr(r, "reason", None) == "empty_context_tick"
        ]
        assert len(suppression_logs) == 0


# ─── Accessor unit tests ──────────────────────────────────────────────────────


class TestAccessors:
    """Unit tests for _has_active_goal_payload and _has_pending_turn."""

    @pytest.mark.asyncio
    async def test_has_active_goal_payload_false_when_empty(self) -> None:
        agent = await _make_agent(goal_progress={})
        assert agent._has_active_goal_payload() is False

    @pytest.mark.asyncio
    async def test_has_active_goal_payload_true_when_nonempty(self) -> None:
        agent = await _make_agent(goal_progress={"ship-v2": 0.3})
        assert agent._has_active_goal_payload() is True

    @pytest.mark.asyncio
    async def test_has_pending_turn_false_when_empty(self) -> None:
        agent = await _make_agent(recent_context=[])
        assert agent._has_pending_turn() is False

    @pytest.mark.asyncio
    async def test_has_pending_turn_true_when_nonempty(self) -> None:
        agent = await _make_agent(recent_context=["some context"])
        assert agent._has_pending_turn() is True


# ─── DB-failure path (tier-exception swallowing) ──────────────────────────────


class TestDBFailurePath:
    """All memory-tier lookups raise → admitted=0 → tick suppressed.

    This class closes the coverage gap flagged in the PR #149 deep review
    (Nice-to-have #1).  The other tests in this file patch
    ``_inject_memory_context`` directly to return a zero-token result,
    which proves the *guard* fires on a zero result but does not prove
    the *upstream swallowing path* actually produces a zero result when
    every tier lookup raises.

    RFC 0017 §F documents — and ``_on_event_inner`` explicitly comments
    on — the design decision that DB failure is intentionally indistinguishable
    from a genuine empty-context tick: both lead to suppression, with
    per-tier ``logger.warning`` calls in ``_inject_memory_context`` as the
    operator-visible signal.  This test pins that contract end-to-end so a
    future refactor that, e.g., re-raises tier exceptions instead of
    swallowing them would fail loudly here.
    """

    @pytest.mark.asyncio
    async def test_all_tier_lookups_raising_suppresses_tick(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Episodic + notes recall both raise → DO_NOTHING, no LLM call, warnings logged.

        TICK events have no ``sender_id`` so ``_inject_memory_context`` never
        invokes the relationship tier (see the ``if sender_id:`` guard in
        ``memory_context.py``).  Patching the two tiers that *are* exercised
        on a TICK is sufficient to drive ``memory_admitted_tokens`` to zero
        through the swallowing path.
        """
        client = _make_client()
        agent = await _make_agent(client=client)

        with (
            patch.object(
                agent._episodic_memory,
                "recall",
                side_effect=OSError("DB unavailable"),
            ),
            patch.object(
                agent._episodic_memory,
                "recall_notes",
                side_effect=OSError("DB unavailable"),
            ),
            caplog.at_level(
                logging.WARNING,
                logger="agents.persona_runtime.memory_context",
            ),
        ):
            actions = await agent.on_event(AgentEvent(event_type=EventType.TICK))

        # Suppression: all exercised tiers raised → admitted=0 → DO_NOTHING.
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.DO_NOTHING
        client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]

        # Operator-visible signal: each failed tier emitted a warning.
        # Use ``getMessage()`` rather than ``.message`` to resolve the
        # ``%s`` placeholders in the format string.
        failure_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "failed, skipping" in r.getMessage()
        ]
        assert len(failure_warnings) >= 2, (
            f"Expected at least 2 tier-failure warnings (episodic + notes), "
            f"got {len(failure_warnings)}: "
            f"{[r.getMessage() for r in failure_warnings]}"
        )
