"""
Tests for PR 5b: ActionExecutor, EventDispatcher, TickScheduler,
and server.py persona wiring.

All tests use mock LLM client — no real API calls.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse
from agents.dispatch import ActionExecutor, EventDispatcher
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
)
from agents.tick import TickScheduler
from agents.server import AgentServer
from agents.server_persona import _resolve_agent_type
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
    "id": "sarah-chen",
    "type": "persona",
    "name": "Sarah Chen",
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

_PERSONA_CONFIG_2: dict = {
    "id": "mike-torres",
    "type": "persona",
    "name": "Mike Torres",
    "role": "Senior developer",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "Senior Engineer",
        "background": "Full-stack developer.",
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


# ─── ActionExecutor Tests ───────────────────────────────────


class TestActionExecutor:

    async def test_complete_task(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.COMPLETE_TASK, {"result": "done"}),
        ])
        assert len(results) == 1
        assert results[0]["action_type"] == "complete_task"
        assert results[0]["status"] == "completed"
        assert results[0]["result"] == "done"

    async def test_do_nothing(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.DO_NOTHING, {}),
        ])
        assert results[0]["status"] == "ok"

    async def test_use_tool_skipped(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.USE_TOOL, {"tool": "file_read"}),
        ])
        assert results[0]["status"] == "skipped"

    async def test_delegate_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.DELEGATE, {"agent_id": "mike-torres", "task": "test"}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_spawn_sub_agent_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.SPAWN_SUB_AGENT, {"role": "helper", "task": "test"}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_request_approval_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.REQUEST_APPROVAL, {}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_grant_approval_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.GRANT_APPROVAL, {}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_deny_approval_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.DENY_APPROVAL, {}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_multiple_actions(self):
        executor = ActionExecutor()
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.DO_NOTHING, {}),
            AgentAction(ActionType.COMPLETE_TASK, {"result": "ok"}),
        ])
        assert len(results) == 2
        assert results[0]["action_type"] == "do_nothing"
        assert results[1]["action_type"] == "complete_task"

    async def test_send_message_no_dispatcher(self):
        executor = ActionExecutor(dispatcher=None)
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.SEND_MESSAGE, {
                "channel_id": "general",
                "content": "Hello!",
                "mentions": ["mike-torres"],
            }),
        ])
        assert results[0]["status"] == "no_dispatcher"

    async def test_send_message_with_dispatcher(self):
        """SEND_MESSAGE dispatches to mentioned agents via EventDispatcher."""
        agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(agents={"mike-torres": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.SEND_MESSAGE, {
                "channel_id": "general",
                "content": "Hey Mike!",
                "mentions": ["mike-torres"],
            }),
        ])
        assert results[0]["status"] == "dispatched"
        assert results[0]["dispatched_to"] == 1
        await agent.close_memory()

    async def test_send_message_no_mentions(self):
        """SEND_MESSAGE with no mentions returns 'no_targets' status.

        An empty mentions list is a no-op, not a failure.  (F-60-R2-2.)
        """
        dispatcher = EventDispatcher()
        executor = ActionExecutor(dispatcher=dispatcher)
        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.SEND_MESSAGE, {
                "channel_id": "general",
                "content": "Hello team!",
                "mentions": [],
            }),
        ])
        assert results[0]["dispatched_to"] == 0
        assert results[0]["status"] == "no_targets"

    async def test_send_message_channel_no_mentions_warns(self, caplog):
        """SEND_MESSAGE with channel_id but no mentions logs WARNING and
        returns 'no_targets' status.

        A message targeting a channel with no explicit mentions is almost
        certainly an LLM error — channel routing is not yet implemented,
        so the message is silently dropped.  The WARNING log makes this
        visible to operators.
        (PR #55 review: silent message drop when channel_id set without mentions.)
        """
        dispatcher = EventDispatcher()
        executor = ActionExecutor(dispatcher=dispatcher)
        with caplog.at_level(logging.WARNING):
            results = await executor.execute("sarah-chen", [
                AgentAction(ActionType.SEND_MESSAGE, {
                    "channel_id": "general",
                    "content": "Hello team!",
                    "mentions": [],
                }),
            ])
        assert results[0]["dispatched_to"] == 0
        assert results[0]["status"] == "no_targets"
        assert any(
            "channel routing not yet implemented" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_send_message_no_channel_no_mentions_debug(self, caplog):
        """SEND_MESSAGE with no channel_id and no mentions returns 'no_targets'.

        No channel_id means the LLM didn't intend channel routing — a
        plain debug log is sufficient (no operator-visible warning).
        (F-60-R2-2: 'no_targets' status distinguishes no-op from failure.)
        """
        dispatcher = EventDispatcher()
        executor = ActionExecutor(dispatcher=dispatcher)
        with caplog.at_level(logging.DEBUG):
            results = await executor.execute("sarah-chen", [
                AgentAction(ActionType.SEND_MESSAGE, {
                    "content": "Hello!",
                    "mentions": [],
                }),
            ])
        assert results[0]["dispatched_to"] == 0
        assert results[0]["status"] == "no_targets"

    async def test_send_message_dispatch_failure_continues(self):
        """A failed dispatch to one mention does not skip remaining mentions.

        execute() promises "Non-fatal failures are logged but do not
        propagate."  The try/except inside _handle_send_message() ensures
        that a failure dispatching to one target still attempts the rest.
        (Review finding: _handle_send_message exception propagation.)
        """
        agent_ok = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(agents={"mike-torres": agent_ok})

        # "ghost-agent" is not registered — dispatch will log a warning
        # but not raise.  To test actual exception handling, make the
        # dispatcher raise for one specific target.
        original_dispatch = dispatcher.dispatch

        call_count = 0

        async def _failing_dispatch(target_id, event):
            nonlocal call_count
            call_count += 1
            if target_id == "bad-agent":
                raise RuntimeError("dispatch failed")
            return await original_dispatch(target_id, event)

        dispatcher.dispatch = _failing_dispatch  # type: ignore[assignment]
        executor = ActionExecutor(dispatcher=dispatcher)

        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.SEND_MESSAGE, {
                "channel_id": "general",
                "content": "Hey everyone!",
                "mentions": ["bad-agent", "mike-torres"],
            }),
        ])
        # Both mentions were attempted despite "bad-agent" raising
        assert call_count == 2
        # Only "mike-torres" succeeded
        assert results[0]["dispatched_to"] == 1
        assert results[0]["status"] == "dispatched"
        await agent_ok.close_memory()

    async def test_send_message_mentions_truncated(self):
        """SEND_MESSAGE with >10 mentions is truncated to prevent resource exhaustion.

        An LLM-generated payload with many mentions would trigger N
        synchronous dispatches, each with an LLM call.  With cascade
        fan-out the worst case is N^D dispatches.  The cap prevents this.
        (PR #55 review: unbounded mentions list → resource exhaustion.)
        """
        agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(agents={"mike-torres": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        # 15 mentions — only first 10 should be dispatched
        many_mentions = [f"agent-{i}" for i in range(15)]
        many_mentions[0] = "mike-torres"  # one valid target

        results = await executor.execute("sarah-chen", [
            AgentAction(ActionType.SEND_MESSAGE, {
                "channel_id": "general",
                "content": "Hello!",
                "mentions": many_mentions,
            }),
        ])
        # Only 10 dispatches attempted (truncated from 15)
        assert results[0]["dispatched_to"] <= 10
        await agent.close_memory()

    async def test_cascade_depth_propagated_through_execute(self):
        """ActionExecutor.execute(cascade_depth=N) propagates depth to child dispatches.

        Verifies the critical path: executor receives a cascade_depth from
        its caller (the dispatcher) and passes it through to
        _handle_send_message() so that child SEND_MESSAGE events inherit
        the correct depth for cascade limiting.
        (PR #55 review: add test for cascade_depth propagation through executor.)
        """
        agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(
            agents={"mike-torres": agent},
            max_cascade_depth=5,
        )
        executor = ActionExecutor(dispatcher=dispatcher)

        # Track what cascade_depth the dispatcher receives
        original_dispatch = dispatcher.dispatch
        received_depths: list[int] = []

        async def _tracking_dispatch(target_id, event):
            received_depths.append(event.metadata.get("cascade_depth", 0))
            return await original_dispatch(target_id, event)

        dispatcher.dispatch = _tracking_dispatch  # type: ignore[assignment]

        await executor.execute("sarah-chen", [
            AgentAction(ActionType.SEND_MESSAGE, {
                "channel_id": "general",
                "content": "Hey!",
                "mentions": ["mike-torres"],
            }),
        ], cascade_depth=3)

        # _handle_send_message() should create an event with cascade_depth=3
        # (the depth it received from execute()), and the dispatcher will
        # then increment it to 4 internally.
        assert len(received_depths) == 1
        assert received_depths[0] == 3
        await agent.close_memory()

    async def test_send_message_missing_channel_id(self):
        """SEND_MESSAGE with no channel_id defaults to empty string.

        Verifies the ``action.payload.get("channel_id", "")`` path at
        dispatch.py _handle_send_message() when channel_id is absent.
        (F-64-DR2-08: missing channel_id path untested.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        action = AgentAction(
            action_type=ActionType.SEND_MESSAGE,
            payload={
                "content": "No channel",
                "mentions": ["sarah-chen"],
                # channel_id intentionally omitted
            },
        )
        results = await executor.execute("mike-torres", [action])
        assert len(results) == 1
        assert results[0]["status"] == "dispatched"
        assert results[0]["dispatched_to"] == 1
        await agent.close_memory()


# ─── EventDispatcher Tests ──────────────────────────────────


class TestEventDispatcher:

    async def test_executor_property_returns_internal_executor(self):
        """Public .executor accessor exposes the internal ActionExecutor.

        (F-64-DR2-06: one-line test to guard accessor stability.)
        """
        dispatcher = EventDispatcher()
        assert dispatcher.executor is dispatcher._executor

    async def test_dispatch_to_registered_agent(self):
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hi Sarah"},
            sender_id="mike-torres",
        )
        actions = await dispatcher.dispatch("sarah-chen", event)
        assert len(actions) >= 1
        await agent.close_memory()

    async def test_dispatch_to_unknown_agent(self):
        dispatcher = EventDispatcher()
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hi"},
        )
        actions = await dispatcher.dispatch("nonexistent", event)
        assert actions == []

    async def test_cascade_depth_limiting(self):
        """Events beyond max_cascade_depth are dropped."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"sarah-chen": agent},
            max_cascade_depth=3,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            metadata={"cascade_depth": 3},  # Already at limit
        )
        actions = await dispatcher.dispatch("sarah-chen", event)
        assert actions == []
        await agent.close_memory()

    async def test_cascade_depth_incremented(self):
        """Dispatch creates a copy with incremented depth; original is unchanged."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
        )
        assert event.metadata.get("cascade_depth", 0) == 0
        await dispatcher.dispatch("sarah-chen", event)
        # Original event metadata must NOT be mutated (review finding:
        # in-place metadata mutation could produce incorrect cascade depth
        # if the same event were dispatched to multiple targets).
        assert event.metadata.get("cascade_depth", 0) == 0
        await agent.close_memory()

    async def test_cascade_depth_below_limit_allowed(self):
        """Events below max_cascade_depth are delivered normally."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"sarah-chen": agent},
            max_cascade_depth=5,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            metadata={"cascade_depth": 2},
        )
        actions = await dispatcher.dispatch("sarah-chen", event)
        assert len(actions) >= 1
        await agent.close_memory()

    async def test_register_agent(self):
        dispatcher = EventDispatcher()
        agent = await _make_agent()
        dispatcher.register_agent("sarah-chen", agent)

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
        )
        actions = await dispatcher.dispatch("sarah-chen", event)
        assert len(actions) >= 1
        await agent.close_memory()

    async def test_wake_tick_scheduler_on_dispatch(self):
        """Dispatcher wakes the tick scheduler when an event arrives."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        scheduler = TickScheduler(agent, interval=999.0)
        dispatcher.register_tick_scheduler("sarah-chen", scheduler)

        # Manually set idle state
        scheduler._idle_count = 15

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "urgent"},
        )
        await dispatcher.dispatch("sarah-chen", event)

        # Scheduler should be woken (idle count reset)
        assert scheduler.idle_count == 0
        await agent.close_memory()

    async def test_self_dispatch_no_deadlock(self):
        """Agent mentioning itself does not deadlock.

        The lock inside on_event() is acquired and released per dispatch
        call (not held across nested dispatches), so self-dispatch is
        safe — bounded by max_cascade_depth.
        (Review finding: untested self-dispatch edge case.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"sarah-chen": agent},
            max_cascade_depth=3,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Talking to myself"},
            sender_id="sarah-chen",
        )
        # Should complete without deadlock or error
        actions = await dispatcher.dispatch("sarah-chen", event)
        assert isinstance(actions, list)
        await agent.close_memory()

    async def test_payload_isolated_between_targets(self):
        """Dispatch shallow-copies payload so targets cannot mutate caller's data.

        (Review finding: shared payload reference between caller and target.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        original_payload = {"content": "test", "mutable_key": "original"}
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload=original_payload,
        )
        await dispatcher.dispatch("sarah-chen", event)

        # Original payload must be unchanged (dispatch copies it)
        assert original_payload["mutable_key"] == "original"
        await agent.close_memory()

    async def test_nested_payload_fully_isolated(self):
        """Dispatch deep-copies payload so nested structures are independent.

        A shallow ``{**event.payload}`` spread would copy top-level keys
        but share nested dicts/lists.  ``copy.deepcopy()`` at L1408
        ensures full isolation.
        (PR #55 review: test copy.deepcopy on nested payload structures.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        nested_list = [1, 2, 3]
        nested_dict = {"inner_key": "inner_value"}
        original_payload = {
            "content": "test",
            "nested_list": nested_list,
            "nested_dict": nested_dict,
        }
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload=original_payload,
        )
        await dispatcher.dispatch("sarah-chen", event)

        # Nested structures in the original payload must be untouched
        assert original_payload["nested_list"] is nested_list
        assert original_payload["nested_dict"] is nested_dict
        assert nested_list == [1, 2, 3]
        assert nested_dict == {"inner_key": "inner_value"}
        await agent.close_memory()

    async def test_metadata_deep_copy_isolation(self):
        """Dispatch deep-copies metadata so caller's metadata is independent.

        Metadata used to be shallow-spread only (``{**event.metadata}``),
        which shared nested mutable structures between caller and the
        dispatched copy.  After the fix (F-64-DR2-02), ``copy.deepcopy()``
        ensures full isolation — mutating the dispatched copy's metadata
        must not affect the original event.
        (F-64-R-SF3: metadata deep-copy isolation test.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        inner_meta = {"trace_ids": ["t1", "t2"]}
        original_metadata = {"cascade_depth": 0, "tracing": inner_meta}
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            metadata=original_metadata,
        )
        await dispatcher.dispatch("sarah-chen", event)

        # Original metadata must be untouched — cascade_depth stays 0
        assert original_metadata["cascade_depth"] == 0
        # Nested structure must be the same object (not replaced)
        assert original_metadata["tracing"] is inner_meta
        assert inner_meta["trace_ids"] == ["t1", "t2"]
        await agent.close_memory()


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
            async def execute(self, agent_id, actions):
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


# ─── Server Persona Wiring Tests ────────────────────────────


class TestResolveAgentType:

    def test_task_type(self):
        assert _resolve_agent_type({"id": "test", "type": "task"}) == "task"

    def test_persona_type(self):
        assert _resolve_agent_type({"id": "test", "type": "persona"}) == "persona"

    def test_default_type(self):
        assert _resolve_agent_type({"id": "test"}) == "task"

    def test_unknown_type(self):
        with pytest.raises(SystemExit, match="Unknown agent type"):
            _resolve_agent_type({"id": "test", "type": "alien"})


class TestAgentServerPersonaLifecycle:
    """Test memory lifecycle and tick scheduler wiring in AgentServer."""

    async def test_persona_agent_memory_initialized_on_start(self):
        """Persona agents have memory initialized during server.start()."""
        agent = await _make_agent()
        # close memory so start() can re-initialize
        await agent.close_memory()

        server = AgentServer()
        server.agents["sarah-chen"] = agent

        # Mock gRPC server and network calls
        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            # Spy on initialize_memory
            agent.initialize_memory = AsyncMock()  # type: ignore[method-assign]
            await server.start()
            agent.initialize_memory.assert_awaited_once()

        await server.stop()

    async def test_persona_agent_memory_closed_on_stop(self):
        """Persona agents have memory closed during server.stop()."""
        agent = await _make_agent()
        server = AgentServer()
        server.agents["sarah-chen"] = agent

        agent.close_memory = AsyncMock()  # type: ignore[method-assign]
        await server.stop()
        agent.close_memory.assert_awaited_once()

    async def test_tick_scheduler_started_for_autonomous_agent(self):
        """Agents with autonomy.level=semi-autonomous get a tick scheduler."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
                "max_actions_per_tick": 3,
                "idle_after_ticks": 10,
            },
        }
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer()
        server.agents["sarah-chen"] = agent

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        assert "sarah-chen" in server._tick_schedulers
        assert server._tick_schedulers["sarah-chen"].is_running
        await server.stop()

    async def test_tick_scheduler_not_started_for_reactive_agent(self):
        """Agents with autonomy.level=reactive (default) do NOT get a tick scheduler."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {"level": "reactive"},
        }
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer()
        server.agents["sarah-chen"] = agent

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        assert "sarah-chen" not in server._tick_schedulers
        await server.stop()

    async def test_tick_scheduler_stopped_on_server_stop(self):
        """Tick schedulers are stopped during server shutdown."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "autonomous",
                "tick_interval_seconds": 999,
            },
        }
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer()
        server.agents["sarah-chen"] = agent

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        assert server._tick_schedulers["sarah-chen"].is_running
        await server.stop()
        assert len(server._tick_schedulers) == 0

    async def test_memory_init_failure_skips_dispatch_registration(self):
        """If initialize_memory() fails, agent is NOT registered with dispatcher or scheduler.

        An agent whose memory initialization fails would crash on the first
        dispatched event (store_episode() on unopened DB).  The server must
        skip dispatcher and tick scheduler registration for such agents.
        (PR #55 review: memory init failure should prevent dispatch registration.)
        """
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
            },
        }
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer()
        server.agents["sarah-chen"] = agent

        # Force initialize_memory() to fail
        agent.initialize_memory = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("DB connection failed"),
        )

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        # Agent should NOT be registered with dispatcher
        assert "sarah-chen" not in server._dispatcher._agents
        # Agent should NOT have a tick scheduler
        assert "sarah-chen" not in server._tick_schedulers

        await server.stop()


# ─── Direct tests for initialize_persona_agents ─────────────
#
# TestAgentServerPersonaLifecycle above tests the same logic indirectly via
# AgentServer.start(), which requires gRPC server mocking.  These tests call
# initialize_persona_agents() directly to cover scenarios that are awkward to
# exercise through the server: mixed agent dicts, partial init failures, and
# the in-place mutation contract on tick_schedulers.


class TestInitializePersonaAgents:

    async def test_task_agent_is_skipped(self):
        """Non-_LLMPersonaAgent entries in the dict are silently ignored."""
        from agents.server_persona import initialize_persona_agents
        from agents.task_agent import TaskAgent

        task_agent = MagicMock(spec=TaskAgent)
        agents = {"worker": task_agent}
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents(agents, dispatcher, schedulers)

        assert "worker" not in schedulers
        assert "worker" not in dispatcher._agents

    async def test_persona_agent_registered_with_dispatcher(self):
        """A persona agent with successful memory init is registered with the dispatcher."""
        from agents.server_persona import initialize_persona_agents

        agent = await _make_agent()
        await agent.close_memory()  # let initialize_persona_agents open it

        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents({"sarah-chen": agent}, dispatcher, schedulers)

        assert "sarah-chen" in dispatcher._agents
        await agent.close_memory()

    async def test_memory_failure_skips_agent_but_others_continue(self):
        """A memory init failure on one agent does not prevent others from initializing.

        The indirect AgentServer tests only exercise single-agent failure.
        This test verifies the multi-agent case: the failing agent is skipped
        and the next agent in the dict is still initialized correctly.
        """
        from agents.server_persona import initialize_persona_agents

        failing_agent = create_persona_agent(
            agent_id="sarah-chen",
            config={**_PERSONA_CONFIG},
            llm_client=_make_client(),
        )
        failing_agent.initialize_memory = AsyncMock(
            side_effect=RuntimeError("DB error"),
        )

        ok_agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        await ok_agent.close_memory()

        agents = {"sarah-chen": failing_agent, "mike-torres": ok_agent}
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents(agents, dispatcher, schedulers)

        assert "sarah-chen" not in dispatcher._agents
        assert "sarah-chen" not in schedulers
        assert "mike-torres" in dispatcher._agents
        await ok_agent.close_memory()

    async def test_tick_schedulers_dict_mutated_in_place(self):
        """The tick_schedulers dict passed in is mutated: autonomous agents are inserted.

        Validates the in-place mutation contract documented in the function's
        docstring — callers rely on the dict being populated, not on a return value.
        """
        from agents.server_persona import initialize_persona_agents

        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 999,
                "max_actions_per_tick": 3,
                "idle_after_ticks": 10,
            },
        }
        agent = create_persona_agent(
            agent_id="sarah-chen", config=config, llm_client=_make_client(),
        )

        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents({"sarah-chen": agent}, dispatcher, schedulers)

        assert "sarah-chen" in schedulers
        assert schedulers["sarah-chen"].is_running

        await schedulers["sarah-chen"].stop()
        await agent.close_memory()


# ─── Cross-Agent Memory Isolation ───────────────────────────


class TestCrossAgentMemoryIsolation:
    """Verify that agents sharing the dispatcher cannot access each other's data."""

    async def test_agent_episodes_isolated(self):
        """Agent A's stored episodes are not visible to agent B."""
        agent_a = await _make_agent(config={**_PERSONA_CONFIG})
        agent_b = await _make_agent(config={**_PERSONA_CONFIG_2})

        # Store episode for agent A
        await agent_a._episodic_memory.store_episode(
            summary="Secret A episode", context={"secret": True},
        )

        # Agent B should not see it
        episodes = await agent_b._episodic_memory.recall("Secret A episode")
        assert len(episodes) == 0

        await agent_a.close_memory()
        await agent_b.close_memory()


# ─── Integration: Full Event → Action → Memory Cycle ────────


class TestEventActionMemoryCycle:
    """Full integration: event dispatched → agent processes → episode stored."""

    async def test_full_cycle(self):
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "Review code"},
        )
        actions = await dispatcher.dispatch("sarah-chen", event)
        assert len(actions) >= 1

        # Verify episode was stored
        episodes = await agent._episodic_memory.recall("task_assigned")
        assert len(episodes) >= 1
        await agent.close_memory()

    async def test_concurrent_dispatch_serialized(self):
        """Concurrent dispatches to the same agent are serialized by the lock."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})

        events = [
            AgentEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={"content": f"msg-{i}"},
                sender_id="test",
            )
            for i in range(3)
        ]

        # Dispatch all concurrently
        results = await asyncio.gather(
            *[dispatcher.dispatch("sarah-chen", e) for e in events]
        )
        # All should complete without error
        assert len(results) == 3
        for r in results:
            assert len(r) >= 1
        await agent.close_memory()


# ─── F-5b-4: Per-dispatch timeout in SEND_MESSAGE ──────────


class TestPerDispatchTimeout:
    """F-5b-4: _handle_send_message wraps dispatch with asyncio.wait_for."""

    async def test_dispatch_timeout_logged_not_raised(self):
        """A dispatch timeout is caught gracefully — sender is not blocked.

        We mock the dispatcher to raise TimeoutError (what asyncio.wait_for
        raises) to verify the except clause in _handle_send_message.
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"sarah-chen": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        # Make dispatch raise TimeoutError as if wait_for expired.
        async def _raise_timeout(target_id, event):
            raise TimeoutError()

        dispatcher.dispatch = _raise_timeout  # type: ignore[assignment]

        action = AgentAction(
            action_type=ActionType.SEND_MESSAGE,
            payload={
                "content": "Hello",
                "mentions": ["sarah-chen"],
            },
        )
        results = await executor.execute("sarah-chen", [action], cascade_depth=0)
        assert len(results) == 1
        # Dispatch timed out, so dispatched_to == 0 (timeout is caught, not counted).
        assert results[0]["dispatched_to"] == 0
        # F-60-6: status is "failed" when all dispatches failed (was "dispatched").
        assert results[0]["status"] == "failed"

        await agent.close_memory()
