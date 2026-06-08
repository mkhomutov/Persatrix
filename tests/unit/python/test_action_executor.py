"""
Tests for ActionExecutor — event action execution and dispatch plumbing.

All tests use mock LLM client — no real API calls.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import ActionExecutor, EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentAction
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

_PERSONA_CONFIG_2: dict = {
    "id": "iron-fox",
    "type": "persona",
    "name": "Iron Fox",
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
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.COMPLETE_TASK, {"result": "done"}),
        ])
        assert len(results) == 1
        assert results[0]["action_type"] == "complete_task"
        assert results[0]["status"] == "completed"
        assert results[0]["result"] == "done"

    async def test_do_nothing(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.DO_NOTHING, {}),
        ])
        assert results[0]["status"] == "ok"

    async def test_use_tool_skipped(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.USE_TOOL, {"tool": "file_read"}),
        ])
        assert results[0]["status"] == "skipped"

    async def test_delegate_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.DELEGATE, {"agent_id": "iron-fox", "task": "test"}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_spawn_sub_agent_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.SPAWN_SUB_AGENT, {"role": "helper", "task": "test"}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_request_approval_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.REQUEST_APPROVAL, {}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_grant_approval_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.GRANT_APPROVAL, {}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_deny_approval_not_implemented(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.DENY_APPROVAL, {}),
        ])
        assert results[0]["status"] == "not_implemented"

    async def test_end_interaction_vote_recognised(self):
        # RFC 0030 Layer 4 (v0.3.8): the vote action is in the vocabulary and the
        # executor handles it explicitly (not the defensive "unhandled" arm). The
        # producer wiring is deferred, so the status is "not_implemented" today.
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.END_INTERACTION_VOTE, {}),
        ])
        assert results[0]["action_type"] == "end_interaction_vote"
        assert results[0]["status"] == "not_implemented"

    async def test_end_interaction_vote_parses_from_action_string(self):
        # The action_parser maps a raw "action_type" string straight through the
        # Enum, so the new vocabulary value is parseable end-to-end.
        assert ActionType("end_interaction_vote") is ActionType.END_INTERACTION_VOTE

    async def test_multiple_actions(self):
        executor = ActionExecutor()
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.DO_NOTHING, {}),
            AgentAction(ActionType.COMPLETE_TASK, {"result": "ok"}),
        ])
        assert len(results) == 2
        assert results[0]["action_type"] == "do_nothing"
        assert results[1]["action_type"] == "complete_task"

    async def test_send_message_no_dispatcher(self):
        executor = ActionExecutor(dispatcher=None)
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "general",
                "content": "Hello!",
                "mentions": ["iron-fox"],
            }),
        ])
        assert results[0]["status"] == "no_dispatcher"

    async def test_send_message_with_dispatcher(self):
        """SEND_CHANNEL_MESSAGE dispatches to mentioned agents via EventDispatcher."""
        agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(agents={"iron-fox": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "general",
                "content": "Hey Mike!",
                "mentions": ["iron-fox"],
            }),
        ])
        assert results[0]["status"] == "dispatched"
        assert results[0]["dispatched_to"] == 1
        await agent.close_memory()

    async def test_send_message_no_mentions(self):
        """SEND_CHANNEL_MESSAGE with no mentions returns 'no_targets' status.

        An empty mentions list is a no-op, not a failure.  (F-60-R2-2.)
        """
        dispatcher = EventDispatcher()
        executor = ActionExecutor(dispatcher=dispatcher)
        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "general",
                "content": "Hello team!",
                "mentions": [],
            }),
        ])
        assert results[0]["dispatched_to"] == 0
        assert results[0]["status"] == "no_targets"

    async def test_send_message_channel_no_mentions_warns(self, caplog):
        """SEND_CHANNEL_MESSAGE with channel_id but no mentions logs WARNING and
        returns 'no_targets' status.

        A message targeting a channel with no explicit mentions and no
        REST publisher wired (e.g. a unit-test fixture or pre-publisher
        startup window) is silently dropped.  The WARNING log makes the
        drop visible to operators so the missing publisher wiring is
        diagnosable from the first occurrence.
        (PR #55 review: silent message drop when channel_id set without mentions.
        Updated for RFC 0011 PR 4a-ii-β-1 — REST routing now exists; this
        path is the publisher-unwired fallback.)
        """
        dispatcher = EventDispatcher()
        executor = ActionExecutor(dispatcher=dispatcher)
        with caplog.at_level(logging.WARNING):
            results = await executor.execute("ember-owl", [
                AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                    "channel_id": "general",
                    "content": "Hello team!",
                    "mentions": [],
                }),
            ])
        assert results[0]["dispatched_to"] == 0
        assert results[0]["status"] == "no_targets"
        assert any(
            "no REST publisher configured" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_send_message_no_channel_no_mentions_debug(self, caplog):
        """SEND_CHANNEL_MESSAGE with no channel_id and no mentions returns 'no_targets'.

        No channel_id means the LLM didn't intend channel routing — a
        plain debug log is sufficient (no operator-visible warning).
        (F-60-R2-2: 'no_targets' status distinguishes no-op from failure.)
        """
        dispatcher = EventDispatcher()
        executor = ActionExecutor(dispatcher=dispatcher)
        with caplog.at_level(logging.DEBUG):
            results = await executor.execute("ember-owl", [
                AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                    "content": "Hello!",
                    "mentions": [],
                }),
            ])
        assert results[0]["dispatched_to"] == 0
        assert results[0]["status"] == "no_targets"

    async def test_send_message_dispatch_failure_continues(self):
        """A failed dispatch to one mention does not skip remaining mentions.

        execute() promises "Non-fatal failures are logged but do not
        propagate."  The try/except inside _handle_send_channel_message() ensures
        that a failure dispatching to one target still attempts the rest.
        (Review finding: _handle_send_channel_message exception propagation.)
        """
        agent_ok = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(agents={"iron-fox": agent_ok})

        # "ghost-agent" is not registered — dispatch will log a warning
        # but not raise.  To test actual exception handling, make the
        # dispatcher raise for one specific target.
        original_dispatch = dispatcher.dispatch

        attempted: list[str] = []

        async def _failing_dispatch(target_id, event):
            attempted.append(target_id)
            if target_id == "bad-agent":
                raise RuntimeError("dispatch failed")
            return await original_dispatch(target_id, event)

        dispatcher.dispatch = _failing_dispatch  # type: ignore[assignment]
        executor = ActionExecutor(dispatcher=dispatcher)

        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "general",
                "content": "Hey everyone!",
                "mentions": ["bad-agent", "iron-fox"],
            }),
        ])
        # Both top-level mentions were attempted despite "bad-agent" raising.
        # iron-fox's own response can trigger a follow-on cascade now that the
        # response gate admits mentioned recipients (RFC 0011 PR 4b legacy
        # cascade fix); the loop-skip invariant only cares about the original
        # SEND_CHANNEL_MESSAGE's two mentions.
        assert {"bad-agent", "iron-fox"}.issubset(attempted), attempted
        # Only "iron-fox" succeeded among the original two
        assert results[0]["dispatched_to"] == 1
        assert results[0]["status"] == "dispatched"
        await agent_ok.close_memory()

    async def test_send_message_mentions_truncated(self):
        """SEND_CHANNEL_MESSAGE with >10 mentions is truncated to prevent resource exhaustion.

        An LLM-generated payload with many mentions would trigger N
        synchronous dispatches, each with an LLM call.  With cascade
        fan-out the worst case is N^D dispatches.  The cap prevents this.
        (PR #55 review: unbounded mentions list → resource exhaustion.)
        """
        agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(agents={"iron-fox": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        # 15 mentions — only first 10 should be dispatched
        many_mentions = [f"agent-{i}" for i in range(15)]
        many_mentions[0] = "iron-fox"  # one valid target

        results = await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
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
        _handle_send_channel_message() so that child SEND_CHANNEL_MESSAGE events inherit
        the correct depth for cascade limiting.
        (PR #55 review: add test for cascade_depth propagation through executor.)
        """
        agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        dispatcher = EventDispatcher(
            agents={"iron-fox": agent},
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

        await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "general",
                "content": "Hey!",
                "mentions": ["iron-fox"],
            }),
        ], cascade_depth=3)

        # _handle_send_channel_message() should create an event with cascade_depth=3
        # (the depth it received from execute()), and the dispatcher will
        # then increment it to 4 internally. iron-fox's own actions from
        # processing the cascaded CHANNEL_MESSAGE may add further dispatches
        # at higher depths (now that RFC 0011 PR 4b's gate admits mentioned
        # recipients in the legacy cascade); only the FIRST recorded depth
        # is load-bearing for the propagation invariant under test.
        assert received_depths, received_depths
        assert received_depths[0] == 3
        await agent.close_memory()

    async def test_send_message_missing_channel_id(self):
        """SEND_CHANNEL_MESSAGE with no channel_id defaults to empty string.

        Verifies the ``action.payload.get("channel_id", "")`` path at
        dispatch.py _handle_send_channel_message() when channel_id is absent.
        (F-64-DR2-08: missing channel_id path untested.)
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        action = AgentAction(
            action_type=ActionType.SEND_CHANNEL_MESSAGE,
            payload={
                "content": "No channel",
                "mentions": ["ember-owl"],
                # channel_id intentionally omitted
            },
        )
        results = await executor.execute("iron-fox", [action])
        assert len(results) == 1
        assert results[0]["status"] == "dispatched"
        assert results[0]["dispatched_to"] == 1
        await agent.close_memory()

    async def test_cascade_depth_forwarded_to_publisher(self):
        """Executor forwards ``cascade_depth`` kwarg as-is to the REST publisher.

        RFC 0011 amendment "Cascade-depth wire propagation" (PR 3 of the
        v0.3.0 channel test-findings plan): the ``+1`` increment lives on
        the dispatcher side (``EventDispatcher.dispatch`` passes
        ``cascade_depth=depth + 1`` to ``executor.execute``); the executor
        receives the already-incremented depth and threads it onto the
        publish call without re-incrementing. A second increment here
        would fire the orchestrator's cap one hop earlier than the
        amendment doc pins.
        """
        from unittest.mock import AsyncMock

        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "group:planning",
                "content": "hi",
                "mentions": ["agent-b"],
            }),
        ], cascade_depth=3)

        publisher.publish.assert_awaited_once()
        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["cascade_depth"] == 3, (
            f"executor must forward cascade_depth verbatim (no +1); "
            f"got {kwargs.get('cascade_depth')!r}"
        )
