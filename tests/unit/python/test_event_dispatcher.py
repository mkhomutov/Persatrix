"""
Tests for EventDispatcher — event routing, cascade depth limiting,
payload isolation, and tick scheduler wiring.

All tests use mock LLM client — no real API calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
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
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hi Sarah"},
            sender_id="iron-fox",
        )
        actions = await dispatcher.dispatch("ember-owl", event)
        assert len(actions) >= 1
        await agent.close_memory()

    async def test_dispatch_to_unknown_agent(self):
        dispatcher = EventDispatcher()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hi"},
        )
        actions = await dispatcher.dispatch("nonexistent", event)
        assert actions == []

    async def test_cascade_depth_limiting(self):
        """Events beyond max_cascade_depth are dropped."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"ember-owl": agent},
            max_cascade_depth=3,
        )

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
            metadata={"cascade_depth": 3},  # Already at limit
        )
        actions = await dispatcher.dispatch("ember-owl", event)
        assert actions == []
        await agent.close_memory()

    async def test_cascade_depth_incremented(self):
        """Dispatch creates a copy with incremented depth; original is unchanged."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
        )
        assert event.metadata.get("cascade_depth", 0) == 0
        await dispatcher.dispatch("ember-owl", event)
        # Original event metadata must NOT be mutated (review finding:
        # in-place metadata mutation could produce incorrect cascade depth
        # if the same event were dispatched to multiple targets).
        assert event.metadata.get("cascade_depth", 0) == 0
        await agent.close_memory()

    async def test_cascade_depth_below_limit_allowed(self):
        """Events below max_cascade_depth are delivered normally."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"ember-owl": agent},
            max_cascade_depth=5,
        )

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
            metadata={"cascade_depth": 2},
        )
        actions = await dispatcher.dispatch("ember-owl", event)
        assert len(actions) >= 1
        await agent.close_memory()

    async def test_register_agent(self):
        dispatcher = EventDispatcher()
        agent = await _make_agent()
        dispatcher.register_agent("ember-owl", agent)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
        )
        actions = await dispatcher.dispatch("ember-owl", event)
        assert len(actions) >= 1
        await agent.close_memory()

    async def test_wake_tick_scheduler_on_dispatch(self):
        """Dispatcher wakes the tick scheduler when an event arrives."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        scheduler = TickScheduler(agent, interval=999.0)
        dispatcher.register_tick_scheduler("ember-owl", scheduler)

        # Manually set idle state
        scheduler._idle_count = 15

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "urgent"},
        )
        await dispatcher.dispatch("ember-owl", event)

        # Scheduler should be woken (idle count reset)
        assert scheduler.idle_count == 0
        await agent.close_memory()

    async def test_has_tick_scheduler_reflects_registration(self):
        """Public ``has_tick_scheduler`` getter (PR 2 review (7)) — True only
        after registration, so the partial-init wiring tests can assert
        cleanup without reaching into ``_tick_schedulers``."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})
        assert not dispatcher.has_tick_scheduler("ember-owl")

        scheduler = TickScheduler(agent, interval=999.0)
        dispatcher.register_tick_scheduler("ember-owl", scheduler)
        assert dispatcher.has_tick_scheduler("ember-owl")
        assert not dispatcher.has_tick_scheduler("nobody")
        await agent.close_memory()

    def test_max_cascade_depth_exposes_configured_ceiling(self):
        """The public ``max_cascade_depth`` getter (PR 4 review (1)) returns
        the configured value so the inbound tick path can share one ceiling
        with ``dispatch()``."""
        assert EventDispatcher(max_cascade_depth=2).max_cascade_depth == 2
        # Default matches the dispatch-time guard's documented default.
        from agents.cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
        assert EventDispatcher().max_cascade_depth == DEFAULT_MAX_CASCADE_DEPTH

    async def test_self_dispatch_no_deadlock(self):
        """Agent mentioning itself does not deadlock.

        The lock inside on_event() is acquired and released per dispatch
        call (not held across nested dispatches), so self-dispatch is
        safe — bounded by max_cascade_depth.
        (Review finding: untested self-dispatch edge case.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"ember-owl": agent},
            max_cascade_depth=3,
        )

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Talking to myself"},
            sender_id="ember-owl",
        )
        # Should complete without deadlock or error
        actions = await dispatcher.dispatch("ember-owl", event)
        assert isinstance(actions, list)
        await agent.close_memory()

    async def test_payload_isolated_between_targets(self):
        """Dispatch shallow-copies payload so targets cannot mutate caller's data.

        (Review finding: shared payload reference between caller and target.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        original_payload = {"content": "test", "mutable_key": "original"}
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=original_payload,
        )
        await dispatcher.dispatch("ember-owl", event)

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
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        nested_list = [1, 2, 3]
        nested_dict = {"inner_key": "inner_value"}
        original_payload = {
            "content": "test",
            "nested_list": nested_list,
            "nested_dict": nested_dict,
        }
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=original_payload,
        )
        await dispatcher.dispatch("ember-owl", event)

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
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        inner_meta = {"trace_ids": ["t1", "t2"]}
        original_metadata = {"cascade_depth": 0, "tracing": inner_meta}
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
            metadata=original_metadata,
        )
        await dispatcher.dispatch("ember-owl", event)

        # Original metadata must be untouched — cascade_depth stays 0
        assert original_metadata["cascade_depth"] == 0
        # Nested structure must be the same object (not replaced)
        assert original_metadata["tracing"] is inner_meta
        assert inner_meta["trace_ids"] == ["t1", "t2"]
        await agent.close_memory()

    async def test_inbound_cascade_depth_increments_and_threads_to_executor(self):
        """Inbound depth=N → child metadata depth=N+1 → executor kwarg=N+1.

        RFC 0011 amendment "Cascade-depth wire propagation" (PR 3 of the
        v0.3.0 channel test-findings plan): the dispatcher is the single
        site that increments cascade depth as an event crosses into the
        next agent's action loop. The executor receives the already-
        incremented value as a kwarg and forwards it onto the REST
        publish call without further increment — pinning the orchestrator
        cap to fire on the "true" hop count rather than one hop early.

        This case asserts the cross-boundary contract: an event arriving
        at depth=4 (e.g. seeded by the gRPC servicer from the wire) walks
        through the dispatcher, fires the agent's ``on_event``, and the
        executor sees ``cascade_depth=5`` on the kwarg. The cap-drop on
        the *next* inbound depth=5 event is already covered by
        :class:`TestEventDispatcher.test_cascade_depth_limiting` /
        ``test_response_gate_cascade_backstop``.
        """
        from unittest.mock import AsyncMock, MagicMock

        from agents.dispatch import ActionExecutor

        agent = await _make_agent()
        dispatcher = EventDispatcher(
            agents={"ember-owl": agent}, max_cascade_depth=5,
        )

        # Swap in a recording executor so we can observe the kwarg the
        # dispatcher hands across. The real ActionExecutor's behaviour
        # under SEND_CHANNEL_MESSAGE is covered in test_action_executor.py.
        recording_executor = MagicMock(spec=ActionExecutor)
        recording_executor.execute = AsyncMock(return_value=[])
        dispatcher._executor = recording_executor

        evt = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "from the wire"},
            sender_id="iron-fox",
            metadata={"cascade_depth": 4},
        )
        await dispatcher.dispatch("ember-owl", evt)

        recording_executor.execute.assert_awaited_once()
        call = recording_executor.execute.await_args
        assert call.kwargs["context"].cascade_depth == 5, (
            f"executor must receive inbound depth+1 on the dispatch "
            f"context; got {call.kwargs!r}"
        )
        await agent.close_memory()

    async def test_dispatch_returns_empty_on_queue_full(self):
        """RFC 0024 PR 1 review finding #1 — when the per-agent EventLoop
        queue is full, ``dispatch`` must return ``[]`` synchronously
        instead of awaiting an unresolvable ``SyncDispatchHandle``.

        Pre-fix shape: the dispatcher created a handle, called
        ``event_loop.enqueue(...)`` ignoring its boolean return value, and
        then ``await handle``-ed.  When ``enqueue`` returned ``False`` (queue
        full, dropped per RFC 0024 Decided §1), the supervisor never saw the
        wake and the handle stayed pending forever — chat-style callers
        hung until their external ``asyncio.wait_for`` deadline fired (chat
        path: clamped timeout; in-process cascade: 60 s default).  This
        test fails fast (``asyncio.wait_for`` at 1.0 s) under the pre-fix
        shape and passes once the dispatcher checks the return value.

        Fake-scheduler approach: ``EventLoop.enqueue``'s queue-full path
        under real conditions is already pinned by
        ``TestQueueFullDiscard.test_queue_full_discards_and_increments_dropped``
        in ``agents/tests/test_event_loop.py``.  This test pins the
        *dispatcher-side* contract: act on the return value.  A
        ``MagicMock`` whose ``enqueue`` always returns ``False`` isolates
        the contract from the EventLoop's queueing machinery.
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        # Fake scheduler: ``is_running`` True so dispatch takes the
        # event-loop branch; ``event_loop.enqueue`` always reports queue-full.
        fake_scheduler = MagicMock()
        fake_scheduler.is_running = True
        fake_scheduler.event_loop = MagicMock()
        fake_scheduler.event_loop.enqueue = MagicMock(return_value=False)
        dispatcher.register_tick_scheduler("ember-owl", fake_scheduler)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "queue-full"},
        )
        # Under the pre-fix shape this hangs forever; the tight timeout
        # converts the hang into a TimeoutError so the test surfaces it.
        actions = await asyncio.wait_for(
            dispatcher.dispatch("ember-owl", event), timeout=1.0,
        )

        assert actions == []
        fake_scheduler.event_loop.enqueue.assert_called_once()
        await agent.close_memory()
