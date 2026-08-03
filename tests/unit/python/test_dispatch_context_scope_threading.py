"""ISSUE-0118 (v0.3.13 PR 1) — per-request epoch/session across the executor hop.

``on_event`` binds the per-request scopes task-locally for its own lifetime
(``request_scope_from_metadata``), but the :class:`ActionExecutor` runs on
the DISPATCHING task — across the queued ``EventLoop`` hop the handler's
ContextVars never reach it — so before this PR every executor-side memory
read/write (the end-vote close discharge persisting the voter's
interaction, a legacy cascade child's recall) resolved the tiers'
construction snapshots: boot epoch ``live`` / legacy session, the F-3
fallback ISSUE-0118 pins.  The fix threads the axes the classification way
(RFC 0037 PR 7 / #788): lifted structurally by
``DispatchContext.for_event`` off the SAME metadata keys the handler-side
binders read, re-entered around action processing by
``DispatchContext.request_scopes``.

The drift guard here ties the executor-side lift to the handler-side
binding through the shared leaf readers, so the two consumers of each
metadata key cannot diverge again — the exact drift that let the epoch
axis skip the executor hop while the classification axis (threaded for
the tripwire in #788) did not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest

from agents.acting_classification import acting_classification_scope_from_metadata
from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.channel_event_classification import CHANNEL_CLASSIFICATION_METADATA_KEY
from agents.dispatch import ActionExecutor, EventDispatcher
from agents.dispatch_context import DispatchContext
from agents.epoch_id import (
    EPOCH_METADATA_GRPC_KEY,
    EVENT_EPOCH_METADATA_KEY,
    current_epoch_id,
    epoch_scope,
    resolve_epoch_id_silent,
)
from agents.generated import task_pb2
from agents.llm_client import LLMResponse, LLMToolResult, StopReason, ToolCall, Usage
from agents.memory.episodic import EpisodicMemory
from agents.persona import create_persona_agent
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.request_scope import request_scope_from_metadata
from agents.server_servicers import AgentServiceServicer
from agents.session_id import (
    EVENT_SESSION_METADATA_KEY,
    SESSION_METADATA_GRPC_KEY,
    current_session_id,
)
from agents.tools.builtin import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Harness ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """``create_memory_tools`` registers globally — isolate each test."""
    clear_registry()
    yield
    clear_registry()


def _event(metadata: dict[str, object] | None = None) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hi"},
        channel_id="group:general",
        sender_id="iron-fox",
        metadata=dict(metadata or {}),
    )


def _scoped_metadata() -> dict[str, object]:
    return {
        EVENT_EPOCH_METADATA_KEY: "mt-crossroom-fresh",
        EVENT_SESSION_METADATA_KEY: "conv-x",
    }


def _tool_func(tools, name: str):
    """The named tool's callable, narrowed for mypy (``func`` is Optional
    on :class:`~agents.tools.registry.ToolDefinition`)."""
    func = next(t for t in tools if t.name == name).func
    assert func is not None
    return func


def _acting_internal():
    """Bind an ``internal`` acting classification for a note store/recall
    pair: an unbound store stamps ``internal`` (rule (a) — never
    ``public``) while an unbound RECALL floors to ``public`` (rule (b)),
    so without this the §D read surface hides every test note and the
    epoch assertions would pass vacuously on empty sets."""
    return acting_classification_scope_from_metadata(
        {CHANNEL_CLASSIFICATION_METADATA_KEY: "internal"},
    )


# ─── The structural lift (for_event) + the drift guard ──────


class TestForEventLiftsScopes:
    def test_lifts_epoch_and_session_from_metadata(self) -> None:
        ctx = DispatchContext.for_event(_event(_scoped_metadata()), cascade_depth=1)
        assert ctx.origin_epoch_id == "mt-crossroom-fresh"
        assert ctx.origin_session_id == "conv-x"

    def test_absent_keys_lift_empty(self) -> None:
        ctx = DispatchContext.for_event(_event(), cascade_depth=1)
        assert ctx.origin_epoch_id == ""
        assert ctx.origin_session_id == ""

    def test_non_string_and_blank_values_lift_empty(self) -> None:
        ctx = DispatchContext.for_event(
            _event({EVENT_EPOCH_METADATA_KEY: 7, EVENT_SESSION_METADATA_KEY: ""}),
            cascade_depth=1,
        )
        assert ctx.origin_epoch_id == ""
        assert ctx.origin_session_id == ""

    def test_drift_guard_lift_agrees_with_handler_binders(self) -> None:
        """The executor-side lift and the handler-side scope binding read
        each axis through the SAME leaf reader — for any metadata, what
        ``request_scope_from_metadata`` binds is what ``for_event`` lifts.
        This is the tie to the classification threading (#788): one rail,
        two consumers, no second key spelling that can drift alone."""
        metadata = _scoped_metadata()
        ctx = DispatchContext.for_event(_event(metadata), cascade_depth=1)
        with request_scope_from_metadata(metadata):
            assert current_epoch_id() == ctx.origin_epoch_id
            assert current_session_id() == ctx.origin_session_id

    def test_event_less_context_defaults_empty(self) -> None:
        assert DispatchContext().origin_epoch_id == ""
        assert DispatchContext().origin_session_id == ""

    def test_reexport_from_channel_wire_metadata_is_same_class(self) -> None:
        """The move to ``dispatch_context.py`` (500-line cap) keeps the
        historical import surface working."""
        from agents.channel_wire_metadata import DispatchContext as Reexported

        assert Reexported is DispatchContext


# ─── request_scopes re-entry ────────────────────────────────


class TestRequestScopesReentry:
    def test_reenters_both_axes(self) -> None:
        ctx = DispatchContext(
            origin_epoch_id="mt-crossroom-fresh", origin_session_id="conv-x",
        )
        assert current_epoch_id() is None
        with ctx.request_scopes():
            assert current_epoch_id() == "mt-crossroom-fresh"
            assert current_session_id() == "conv-x"
        assert current_epoch_id() is None
        assert current_session_id() is None

    def test_empty_fields_enter_nothing(self) -> None:
        with DispatchContext().request_scopes():
            assert current_epoch_id() is None
            assert current_session_id() is None

    def test_one_sided_entry(self) -> None:
        with DispatchContext(origin_epoch_id="e-only").request_scopes():
            assert current_epoch_id() == "e-only"
            assert current_session_id() is None
        with DispatchContext(origin_session_id="s-only").request_scopes():
            assert current_epoch_id() is None
            assert current_session_id() == "s-only"

    def test_restores_on_exception(self) -> None:
        ctx = DispatchContext(origin_epoch_id="e1", origin_session_id="s1")
        with pytest.raises(RuntimeError):
            with ctx.request_scopes():
                raise RuntimeError("boom")
        assert current_epoch_id() is None
        assert current_session_id() is None


# ─── The executor hop ───────────────────────────────────────


class TestExecutorReentersScopes:
    async def test_actions_execute_under_the_request_scopes(self) -> None:
        """Before this PR the executor's action processing ran with NO
        request scopes — ``current_epoch_id()``/``current_session_id()``
        were ``None`` here and every memory seam below it fell back to
        construction snapshots (the red state this test pinned)."""
        executor = ActionExecutor()
        seen: list[tuple[str | None, str | None]] = []

        async def _spy(agent_id: str, action: AgentAction, *, context: DispatchContext):
            seen.append((current_epoch_id(), current_session_id()))
            return {"action_type": "do_nothing", "status": "ok"}

        executor._execute_one = AsyncMock(side_effect=_spy)  # type: ignore[method-assign]
        await executor.execute(
            "ember-owl", [AgentAction(ActionType.DO_NOTHING, {})],
            context=DispatchContext(
                origin_epoch_id="mt-crossroom-fresh", origin_session_id="conv-x",
            ),
        )
        assert seen == [("mt-crossroom-fresh", "conv-x")]

    async def test_scope_less_context_keeps_snapshot_fallback(self) -> None:
        """An event-less / tick / pre-rail context threads nothing — the
        construction-snapshot fallback every scope axis ships with."""
        executor = ActionExecutor()
        seen: list[tuple[str | None, str | None]] = []

        async def _spy(agent_id: str, action: AgentAction, *, context: DispatchContext):
            seen.append((current_epoch_id(), current_session_id()))
            return {"action_type": "do_nothing", "status": "ok"}

        executor._execute_one = AsyncMock(side_effect=_spy)  # type: ignore[method-assign]
        await executor.execute(
            "ember-owl", [AgentAction(ActionType.DO_NOTHING, {})],
            context=DispatchContext(),
        )
        assert seen == [(None, None)]

    async def test_tool_recall_on_executor_leg_resolves_request_epoch(self) -> None:
        """The issue's namesake shape, deterministic: a memory-tool recall
        running on the executor's side of the hop.  Two notes exist — one
        in the construction world, one in a fresh epoch.  Under a context
        carrying the fresh epoch the recall must return ONLY the fresh
        note; before the threading it resolved the construction snapshot
        and returned the construction-world note (the live 2026-07-30
        leak's mechanism class)."""
        memory = EpisodicMemory(agent_id="ember-owl", db_path=":memory:")
        await memory.initialize()
        try:
            gate = PermissionGate({"memory": {"read": True, "write": True}})
            tools = create_memory_tools(memory, gate)
            recall_notes = _tool_func(tools, "recall_notes")
            store_note = _tool_func(tools, "store_note")

            with _acting_internal():
                # Construction world (no epoch scope active).
                await store_note(topic="atlas", content="Atlas ships Friday")
                # Fresh-epoch world.
                with epoch_scope("mt-crossroom-fresh"):
                    await store_note(
                        topic="atlas", content="fresh world is empty-ish",
                    )

            executor = ActionExecutor()
            recalled: list[list[dict[str, object]]] = []

            async def _tool_arm(
                agent_id: str, action: AgentAction, *, context: DispatchContext,
            ):
                with _acting_internal():
                    result = await recall_notes(query="atlas")
                assert result.success is True
                recalled.append(result.data)
                return {"action_type": "do_nothing", "status": "ok"}

            executor._execute_one = AsyncMock(side_effect=_tool_arm)  # type: ignore[method-assign]
            await executor.execute(
                "ember-owl", [AgentAction(ActionType.DO_NOTHING, {})],
                context=DispatchContext(origin_epoch_id="mt-crossroom-fresh"),
            )

            contents = [row["content"] for row in recalled[0]]
            assert contents == ["fresh world is empty-ish"]
        finally:
            await memory.close()


class TestLegacyCascadeChildCarriesScopes:
    """The legacy in-process cascade's synthesized child event must carry
    the origin epoch/session on the metadata rail: a child routed through
    the target's queued ``EventLoop`` runs on the supervisor task, where
    the scopes re-entered around ``execute()`` cannot follow — the
    metadata keys are what crosses that hop (the child's own ``on_event``
    binds them)."""

    @staticmethod
    def _executor_with_recording_dispatcher() -> tuple[ActionExecutor, list[AgentEvent]]:
        dispatched: list[AgentEvent] = []

        class _RecordingDispatcher:
            async def dispatch(self, target_id: str, event: AgentEvent):
                dispatched.append(event)
                return []

        executor = ActionExecutor(dispatcher=_RecordingDispatcher())  # type: ignore[arg-type]
        return executor, dispatched

    async def test_child_event_metadata_carries_origin_scopes(self) -> None:
        executor, dispatched = self._executor_with_recording_dispatcher()
        await executor.execute(
            "ember-owl",
            [AgentAction(ActionType.SEND_CHANNEL_MESSAGE,
                         {"content": "hi", "mentions": ["iron-fox"]})],
            context=DispatchContext(
                cascade_depth=1,
                origin_epoch_id="mt-crossroom-fresh", origin_session_id="conv-x",
            ),
        )
        assert len(dispatched) == 1
        md = dispatched[0].metadata
        assert md[EVENT_EPOCH_METADATA_KEY] == "mt-crossroom-fresh"
        assert md[EVENT_SESSION_METADATA_KEY] == "conv-x"

    async def test_scope_less_context_seeds_no_keys(self) -> None:
        """Key-ABSENCE is the binders' nullcontext contract — an unscoped
        cascade must not grow the keys."""
        executor, dispatched = self._executor_with_recording_dispatcher()
        await executor.execute(
            "ember-owl",
            [AgentAction(ActionType.SEND_CHANNEL_MESSAGE,
                         {"content": "hi", "mentions": ["iron-fox"]})],
            context=DispatchContext(cascade_depth=1),
        )
        md = dispatched[0].metadata
        assert EVENT_EPOCH_METADATA_KEY not in md
        assert EVENT_SESSION_METADATA_KEY not in md


# ─── The chat surface's post-reply executor leg ─────────────


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


class TestSendChatMessageThreadsScopes:
    """``SendChatMessage`` executes side-effect actions AFTER extracting
    the reply — on the servicer task, after ``on_event``'s binding exited
    — so its origin-less context must still carry the request's
    epoch/session for the executor's re-entry."""

    @staticmethod
    def _servicer() -> tuple[AgentServiceServicer, MagicMock]:
        agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.dispatch = AsyncMock(return_value=[
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE,
                        {"content": "hi", "mentions": ["local"]}),
        ])
        dispatcher.executor = MagicMock()
        dispatcher.executor.execute = AsyncMock(return_value=[])
        return AgentServiceServicer({"ember-owl": agent}, dispatcher), dispatcher

    @staticmethod
    def _context(metadata: list[tuple[str, str]]) -> MagicMock:
        context = MagicMock(spec=grpc.aio.ServicerContext)
        context.invocation_metadata.return_value = metadata
        return context

    async def test_post_reply_execute_context_carries_scopes(self) -> None:
        servicer, dispatcher = self._servicer()
        await servicer.SendChatMessage(
            task_pb2.ChatRequest(agent_id="ember-owl", user_id="local", message="hi"),
            self._context([
                (EPOCH_METADATA_GRPC_KEY, "mt-crossroom-fresh"),
                (SESSION_METADATA_GRPC_KEY, "conv-x"),
            ]),
        )
        ctx = dispatcher.executor.execute.call_args.kwargs["context"]
        assert ctx.origin_epoch_id == "mt-crossroom-fresh"
        assert ctx.origin_session_id == "conv-x"
        # Origin-less by design — the claim posture is unchanged.
        assert ctx.origin_channel_id == ""
        assert ctx.origin_interaction_id == ""

    async def test_headerless_request_threads_nothing(self) -> None:
        servicer, dispatcher = self._servicer()
        await servicer.SendChatMessage(
            task_pb2.ChatRequest(agent_id="ember-owl", user_id="local", message="hi"),
            self._context([]),
        )
        ctx = dispatcher.executor.execute.call_args.kwargs["context"]
        assert ctx.origin_epoch_id == ""
        assert ctx.origin_session_id == ""


# ─── The in-loop tool round stays correctly scoped ──────────


class TestInLoopToolRoundParity:
    """Regression pin for the surface ISSUE-0118 named: a tool recall
    inside ``_on_event_inner``'s multi-turn loop inherits the handler's
    scope binding — the injection path and the tool path agree on the
    request's epoch.  Driven through the REAL machinery (``on_event``
    binds ``request_scope_from_metadata``; ``asyncio.wait_for``'s child
    task copies the ContextVars; ``_execute_tools`` runs the recall)
    with a scripted LLM electing the tool call, so a refactor moving
    tool execution off the handler task flips this red instead of
    shipping the leak silently.  (PR #809 review finding 1: the earlier
    direct-call shape asserted only the memory tiers' scope filtering —
    already covered by the executor-leg test above — and never touched
    the loop it claimed to pin.)"""

    async def test_tool_round_inside_on_event_resolves_request_epoch(self) -> None:
        """Two notes exist — one in the construction world, one in a
        fresh epoch.  An event delivered under the fresh epoch whose
        scripted turn elects ``recall_notes`` must feed the model ONLY
        the fresh note; a binding lost across a future refactor would
        surface the construction-world note instead (the live 2026-07-30
        leak's mechanism class, in-loop edition)."""
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="tc1", name="recall_notes", input={"query": "atlas"},
                )],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(100, 50),
            ),
            LLMResponse(
                text="Nothing notable on atlas.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(200, 100),
            ),
        ]
        client = _make_client(responses)
        # Observe the ACTUAL in-loop results at the append seam — the one
        # place they cross the client boundary — rather than stubbing any
        # part of the loop under test.
        tool_rounds: list[list[LLMToolResult]] = []

        def _capture(
            msgs: list, _resp: LLMResponse, results: list[LLMToolResult],
        ) -> list:
            tool_rounds.append(list(results))
            return [*msgs, {"role": "assistant", "content": "tool round"},
                    {"role": "user", "content": "tool results"}]

        client._provider.append_tool_round = MagicMock(  # type: ignore[method-assign]
            side_effect=_capture,
        )

        agent = create_persona_agent(
            agent_id="ember-owl", config={**_PERSONA_CONFIG}, llm_client=client,
        )
        await agent.initialize_memory()
        try:
            store_note = _tool_func(agent._memory_tools, "store_note")
            with _acting_internal():
                await store_note(topic="atlas", content="Atlas ships Friday")
                with epoch_scope("mt-crossroom-fresh"):
                    await store_note(
                        topic="atlas", content="fresh world is empty-ish",
                    )
            # Vacuous-pass guard: the construction world must actually
            # differ from the probe epoch.
            assert resolve_epoch_id_silent() != "mt-crossroom-fresh"

            actions = await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                # ``respond_policy=always`` with no mentions is the
                # open-floor admit; the channel is ungoverned, so the
                # Tier B salience bid stays out of the turn.
                payload={"content": "any news on atlas?",
                         "respond_policy": "always"},
                channel_id="group:general",
                sender_id="iron-fox",
                metadata={
                    **_scoped_metadata(),
                    CHANNEL_CLASSIFICATION_METADATA_KEY: "internal",
                },
            ))

            assert actions, "scripted END_TURN must still parse into actions"
            assert len(tool_rounds) == 1
            [result] = tool_rounds[0]
            assert result.is_error is False
            assert "fresh world is empty-ish" in result.content
            assert "Atlas ships Friday" not in result.content
        finally:
            await agent.close_memory()
