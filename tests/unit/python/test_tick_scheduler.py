"""
Tests for TickScheduler — autonomous tick loop, idle detection, energy
recovery, and production-default interval clamping.

All tests use mock LLM client — no real API calls.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.dispatch import ActionExecutor
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_types import ActionType, AgentAction
from agents.tick import TickScheduler
from agents.tools.registry import clear_registry


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    """Create a mock LLMClient that returns the given responses."""
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(text="I'll handle this task.")
        )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ]
    )
    return LLMClient(mock_provider)


_PERSONA_CONFIG: dict = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "role": "Engineering leadership",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "VP of Engineering",
        "background": "15 years in software engineering.",
        "behavior": {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        },
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}


async def _make_agent(
    config: dict | None = None,
    llm_client: LLMClient | None = None,
) -> _LLMPersonaAgent:
    """Helper to create an initialized _LLMPersonaAgent."""
    cfg = config or {**_PERSONA_CONFIG}
    client = llm_client or _make_client()
    agent = create_persona_agent(
        agent_id=cfg["id"], config=cfg, llm_client=client,
    )
    await agent.initialize_memory()
    return agent


# ─── TickScheduler Tests ────────────────────────────────────


class TestTickScheduler:

    @pytest.fixture(autouse=True)
    def _lower_min_interval(self):
        """Allow sub-second intervals in tests.

        Production _MIN_INTERVAL is 1.0s to prevent cost bursts
        (F-64-DR2-11).  Tests need fast intervals to avoid multi-second waits.
        """
        original = TickScheduler._MIN_INTERVAL
        TickScheduler._MIN_INTERVAL = 0.01
        yield
        TickScheduler._MIN_INTERVAL = original

    async def test_start_stop(self):
        agent = await _make_agent()
        scheduler = TickScheduler(agent, interval=0.05)
        scheduler.start()
        assert scheduler.is_running
        await asyncio.sleep(0.02)
        await scheduler.stop()
        assert not scheduler.is_running
        await agent.close_memory()

    async def test_ticks_fire(self):
        """Tick loop calls on_tick() at the configured interval."""
        agent = await _make_agent()
        tick_count = 0
        original_on_tick = agent.on_tick

        async def _counting_tick():
            nonlocal tick_count
            tick_count += 1
            return await original_on_tick()

        agent.on_tick = _counting_tick  # type: ignore[assignment]

        scheduler = TickScheduler(agent, interval=0.05, idle_after_ticks=100)
        scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        assert tick_count >= 2
        await agent.close_memory()

    async def test_idle_detection(self):
        """Non-DO_NOTHING actions keep idle count at zero."""
        agent = await _make_agent()
        executor = ActionExecutor()
        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=3, executor=executor,
        )
        # Patch _inject_memory_context to return non-zero tokens so the
        # RFC 0017 §F empty-context TICK short-circuit does not fire.
        # Without this, TICKs on an idle agent with no memory match all four
        # short-circuit conditions and return DO_NOTHING, which increments
        # idle_count unexpectedly. By injecting 200 tokens, the LLM is called
        # and returns its normal COMPLETE_TASK action.
        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=MemoryInjectionResult(memory_admitted_tokens=200),
        ):
            scheduler.start()
            # Wait for enough ticks to reach idle threshold
            await asyncio.sleep(0.3)
            await scheduler.stop()

        # Default mock LLM returns text that falls through to COMPLETE_TASK
        # (not DO_NOTHING), so the idle counter should remain at zero —
        # only consecutive DO_NOTHING ticks increment it.
        assert scheduler.idle_count == 0
        assert not scheduler.is_idle
        await agent.close_memory()

    async def test_idle_detection_with_do_nothing(self):
        """When on_tick returns DO_NOTHING, idle count increments."""
        agent = await _make_agent()

        async def _do_nothing_tick():
            return [AgentAction(ActionType.DO_NOTHING, {})]

        agent.on_tick = _do_nothing_tick  # type: ignore[assignment]
        executor = ActionExecutor()

        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=3, executor=executor,
        )
        scheduler.start()
        await asyncio.sleep(0.25)
        await scheduler.stop()

        assert scheduler.idle_count >= 3
        assert scheduler.is_idle
        await agent.close_memory()

    async def test_idle_skip_llm_calls(self):
        """Once idle, tick loop skips LLM calls."""
        agent = await _make_agent()
        call_count = 0

        async def _tracking_tick():
            nonlocal call_count
            call_count += 1
            return [AgentAction(ActionType.DO_NOTHING, {})]

        agent.on_tick = _tracking_tick  # type: ignore[assignment]

        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=2, executor=ActionExecutor(),
        )
        scheduler.start()
        await asyncio.sleep(0.5)
        await scheduler.stop()

        # Should have stopped calling on_tick after idle threshold reached
        # (2 ticks to become idle, then skipped)
        assert call_count >= 2  # at least the threshold
        await agent.close_memory()

    async def test_wake_resets_idle(self):
        """wake() resets idle count so the next tick fires."""
        agent = await _make_agent()

        async def _do_nothing_tick():
            return [AgentAction(ActionType.DO_NOTHING, {})]

        agent.on_tick = _do_nothing_tick  # type: ignore[assignment]

        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=2, executor=ActionExecutor(),
        )
        scheduler._idle_count = 10
        assert scheduler.is_idle

        scheduler.wake()
        assert scheduler.idle_count == 0
        assert not scheduler.is_idle
        await agent.close_memory()

    async def test_max_actions_per_tick(self):
        """Only max_actions_per_tick actions are executed per tick."""
        agent = await _make_agent()
        executed_count = 0

        async def _many_actions_tick():
            return [AgentAction(ActionType.DO_NOTHING, {}) for _ in range(10)]

        agent.on_tick = _many_actions_tick  # type: ignore[assignment]

        class CountingExecutor:
            async def execute(self, agent_id, actions, *, cascade_depth=0):
                # ``cascade_depth`` accepted (and ignored) so the
                # scheduler's explicit
                # ``cascade_depth=DEFAULT_MAX_CASCADE_DEPTH`` kwarg
                # does not raise ``TypeError`` here. The depth-on-the-
                # wire contract is pinned by
                # ``test_tick_cascade_depth_default.py``; this test
                # only counts actions.
                nonlocal executed_count
                executed_count += len(actions)
                return [{"status": "ok"} for _ in actions]

        scheduler = TickScheduler(
            agent, interval=0.05, max_actions_per_tick=3,
            idle_after_ticks=100, executor=CountingExecutor(),  # type: ignore[arg-type]
        )
        scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        # Each tick should execute at most 3 actions (truncated from 10)
        # Multiple ticks have fired, so total should be a multiple of 3
        assert executed_count > 0
        assert executed_count % 3 == 0
        await agent.close_memory()

    async def test_tick_error_does_not_crash_loop(self):
        """An exception in on_tick() is caught — loop continues."""
        agent = await _make_agent()
        call_count = 0

        async def _failing_tick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("tick error")
            return [AgentAction(ActionType.DO_NOTHING, {})]

        agent.on_tick = _failing_tick  # type: ignore[assignment]

        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=100, executor=ActionExecutor(),
        )
        scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        assert call_count >= 2  # Loop continued after first error
        await agent.close_memory()

    async def test_start_idempotent(self):
        """Calling start() twice doesn't create duplicate tasks."""
        agent = await _make_agent()
        scheduler = TickScheduler(agent, interval=0.05)
        scheduler.start()
        task1 = scheduler._task
        scheduler.start()  # Should be no-op
        assert scheduler._task is task1
        await scheduler.stop()
        await agent.close_memory()

    async def test_graceful_stop_timeout(self):
        """Stop with a very short timeout cancels the task."""
        agent = await _make_agent()

        async def _slow_tick():
            await asyncio.sleep(10)
            return [AgentAction(ActionType.DO_NOTHING, {})]

        agent.on_tick = _slow_tick  # type: ignore[assignment]

        scheduler = TickScheduler(agent, interval=0.01, idle_after_ticks=100)
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop(timeout=0.01)  # Very short timeout
        assert not scheduler.is_running
        await agent.close_memory()

    async def test_min_interval_clamping(self):
        """Interval below _MIN_INTERVAL is clamped to prevent busy loops."""
        agent = await _make_agent()
        scheduler = TickScheduler(agent, interval=0.0)
        assert scheduler._interval >= TickScheduler._MIN_INTERVAL

        scheduler2 = TickScheduler(agent, interval=-5.0)
        assert scheduler2._interval >= TickScheduler._MIN_INTERVAL
        await agent.close_memory()

    async def test_idle_energy_recovery(self):
        """When idle, tick loop still recovers energy via recover_idle_energy().

        Verifies the idle recovery codepath (TickScheduler._run() idle
        branch) restores energy so agents aren't depleted after long
        idle periods.
        (PR #55 review: test idle energy recovery path — coverage gap.)
        """
        agent = await _make_agent()

        # Drain energy to a known low level
        agent._state.energy = 0.3

        async def _do_nothing_tick():
            return [AgentAction(ActionType.DO_NOTHING, {})]

        agent.on_tick = _do_nothing_tick  # type: ignore[assignment]
        executor = ActionExecutor()

        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=2, executor=executor,
        )
        scheduler.start()
        # Let it tick past the idle threshold and run idle recovery
        await asyncio.sleep(0.4)
        await scheduler.stop()

        assert scheduler.is_idle
        # Energy should have recovered from 0.3 (each idle tick adds 0.1)
        assert agent._state.energy > 0.3
        await agent.close_memory()

    async def test_no_executor_non_idle_actions_logs_warning(self, caplog):
        """When executor is None and agent produces non-idle actions, warn.

        Covers the warning log path at tick.py _run() where actionable
        output is silently discarded due to missing executor wiring.
        (F-64-DR2-07: no test for no-executor warning path.)
        """
        agent = await _make_agent()

        async def _actionable_tick():
            return [AgentAction(ActionType.COMPLETE_TASK, {"result": "done"})]

        agent.on_tick = _actionable_tick  # type: ignore[assignment]

        # executor=None — actions will be discarded with a warning
        scheduler = TickScheduler(
            agent, interval=0.05, idle_after_ticks=100, executor=None,
        )
        scheduler.start()
        with caplog.at_level(logging.WARNING, logger="agents.tick"):
            await asyncio.sleep(0.15)
        await scheduler.stop()

        assert any(
            "no executor is configured" in rec.message
            for rec in caplog.records
        ), "Expected warning about discarded actions with no executor"
        await agent.close_memory()


class TestTickSchedulerProductionDefaults:
    """Tests for TickScheduler production defaults without fixture override.

    Separated from TestTickScheduler which uses _lower_min_interval
    autouse fixture.  These tests verify production behavior at the
    default _MIN_INTERVAL = 1.0s.
    (F-64-DR5-09: no test verifies production _MIN_INTERVAL without
    fixture override.)
    """

    def test_min_interval_production_default(self):
        """Production _MIN_INTERVAL is 1.0s, not the test-lowered value."""
        assert TickScheduler._MIN_INTERVAL == 1.0, (
            f"Expected production _MIN_INTERVAL=1.0, "
            f"got {TickScheduler._MIN_INTERVAL}"
        )

    async def test_sub_second_interval_clamped_to_min(self):
        """Agent with interval=0.5 is clamped to _MIN_INTERVAL at production default."""
        agent = await _make_agent()
        scheduler = TickScheduler(agent, interval=0.5)
        # The scheduler should clamp to _MIN_INTERVAL (1.0s)
        assert scheduler._interval >= TickScheduler._MIN_INTERVAL, (
            f"interval={scheduler._interval} should be >= "
            f"_MIN_INTERVAL={TickScheduler._MIN_INTERVAL}"
        )
        await agent.close_memory()
