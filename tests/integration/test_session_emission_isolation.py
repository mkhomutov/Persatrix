"""ISSUE-0082 PR 3 — Go→Python session-emission wire proof + isolation gate.

The acceptance gate for ISSUE-0082 Part 1 (session emission).  ISSUE-0081
built the persona-side rail (header lift in the servicer, ``session_scope``
re-establishment in ``on_event``) and proved its *recall* half at the tier
and facade layers (``tests/unit/python/test_principal_scope.py``,
``tests/integration/test_session_recall_isolation.py``,
``tests/integration/test_session_continuity.py``).  PR 1 + PR 2 of this
issue added the orchestrator-side *source*: a persisted
``(agent, channel, user) → session_id`` binding and its emission as the
``persatrix-session`` gRPC header on the live ``GRPCMessageDispatcher.Dispatch``
path.

What no existing test covers is the **seam between the two halves**: that a
``persatrix-session`` header arriving on a real gRPC ``ReceiveChannelMessage``
call is lifted, threaded onto the event envelope, re-established as the
task-local ``session_scope`` for the handler, and therefore *frozen onto the
interaction* so the persisted episode lands under that session — and that the
RFC 0031 §D recall filter then isolates two concurrent conversations for one
agent while keeping the ``legacy`` carve-out visible to both.

This file drives the **real** ``AgentServiceServicer`` over a real ephemeral
gRPC channel; the client emits the ``persatrix-session`` header exactly as the
Go orchestrator's ``grpcmeta.InjectSession`` does on its outbound dispatch.
The Go-side emission is independently pinned by the PR 2 dispatcher tests
(``internal/channels/grpc_dispatcher_session_test.go``); standing up the real
Go binary from pytest is heavier scaffolding than this contract pin justifies —
the same reasoning as ``test_channel_cascade_backstop_cross_process.py``.  The
header *string* is taken from :data:`agents.session_id.SESSION_METADATA_GRPC_KEY`
— the same constant the servicer lifts — so the emit here can never drift to a
stale hard-coded key while the servicer moves on.  This does **not** by itself
guard the cross-language wire *value*: the emit and the lift resolve through that
one Python constant, so renaming its value would move both in lock-step and keep
this test green.  The Go↔Python agreement on the literal ``"persatrix-session"``
is pinned independently — Python side by
``tests/unit/python/test_session_id_pr2_binding.py``, Go side by
``internal/observability/grpcmeta/grpcmeta_test.go``.

Two layers of pin:

* :class:`TestSessionHeaderBindsInteraction` — the wire pin: the emitted
  header freezes the interaction's session; its absence falls through to the
  construction-time ``legacy`` snapshot (the pre-activation behaviour, the
  negative control that proves the header is what does the work).
* :class:`TestConcurrentConversationIsolation` — the §B/§D acceptance gate:
  two concurrent conversations for one agent recall in isolation, and a
  pre-activation ``legacy`` row stays visible to both.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import scope_for_dm
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
from agents.server_servicers import AgentServiceServicer
from agents.session_id import (
    LEGACY_SESSION_ID,
    SESSION_METADATA_GRPC_KEY,
    session_scope,
)
from agents.tools.registry import clear_registry

# The single agent under test.  ``(agent, channel, user)`` is the RFC 0031 §B
# session unit; with one agent + two DM peers we get two distinct conversations
# whose only discriminator is the session id the orchestrator emits.
_AGENT_ID = "ember-owl"

# A fixed RFC 3339 timestamp — the channel-message validator rejects naive /
# empty timestamps (``agents/channel_validation.py``).  A constant keeps the
# fixture deterministic (and is workflow-safe: no wall-clock read).
_TS = "2026-05-29T12:00:00Z"

# Distinctive marker for the pre-activation ``legacy`` episode so the carve-out
# row is identifiable in recall output without depending on the (mock-derived)
# summary text of the closed conversations.
_LEGACY_MARKER = "pre-activation-legacy-carveout-row"


@pytest.fixture(autouse=True)
def _clean_registry() -> AsyncIterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the construction-time session snapshot to the ``legacy`` carve-out.

    The agent reads ``PERSATRIX_SESSION_ID`` once at construction into
    ``_session_id``; an exported value in the test environment would make the
    "no header" negative control land under that value instead of ``legacy``,
    masking the very fall-through it exists to prove.
    """
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "ISSUE-0082 session-emission isolation pin",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the ISSUE-0082 PR 3 session-emission gate.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "autonomy": {
            "level": "semi-autonomous",
            "tick_interval_seconds": 1,
            "max_actions_per_tick": 3,
            "idle_after_ticks": 5,
        },
        "memory": {
            "db_path": ":memory:",
            "working": {"max_tokens": 50000},
        },
        "relationships": [],
    }


def _do_nothing_client() -> LLMClient:
    """Mock LLM whose every reply parses to a single DO_NOTHING.

    The isolation property under test is keyed on the persisted ``session_id``
    column and the §D recall filter, not on summary *content* — so the
    episode's (mock-derived) summary text is irrelevant.  A do-nothing reply
    keeps the persona from emitting outbound channel traffic (no publisher is
    wired) while still driving the full ingest → close → persist path.
    """
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def _make_agent() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=_AGENT_ID,
        config=_persona_config(_AGENT_ID),
        llm_client=_do_nothing_client(),
    )
    assert isinstance(agent, _LLMPersonaAgent)
    await agent.initialize_memory()
    return agent


def _dm_channel(peer: str) -> str:
    return f"dm:{_AGENT_ID}:{peer}"


def _dm_event(peer: str, content: str, *, message_id: str) -> task_pb2.ChannelMessageEvent:
    """Build a valid DM ``ChannelMessageEvent`` from ``peer`` to the agent.

    DM + ``respond_policy="always"`` clears the response gate so the message
    reaches the memory-ingestion path (the gate's DM override admits it
    regardless of mentions).
    """
    return task_pb2.ChannelMessageEvent(
        message_id=message_id,
        channel_id=_dm_channel(peer),
        channel_type="dm",
        sender_id=peer,
        content=content,
        mentions=[],
        timestamp=_TS,
        respond_policy="always",
        thread_parent_sender_id="",
        cascade_depth=0,
    )


class _GrpcWorld:
    """A real ``AgentServiceServicer`` + client over an ephemeral gRPC port.

    Mirrors the production receiver wiring: ``EventDispatcher`` with no tick
    scheduler, so inbound channel messages take the fire-and-forget no-loop
    fallback (a detached processing task per call).  :meth:`receive` emits the
    ``persatrix-session`` header the way the orchestrator's
    ``grpcmeta.InjectSession`` does, then drains the fallback task so the
    ingest is complete when it returns.
    """

    def __init__(
        self,
        agent: _LLMPersonaAgent,
        dispatcher: EventDispatcher,
        stub: task_pb2_grpc.AgentServiceStub,
    ) -> None:
        self.agent = agent
        self._dispatcher = dispatcher
        self._stub = stub

    async def receive(
        self,
        event: task_pb2.ChannelMessageEvent,
        *,
        session_id: str | None,
    ) -> None:
        """Dispatch one ``ReceiveChannelMessage`` and await its ingest.

        ``session_id=None`` emits no header (the pre-activation shape);
        otherwise the id rides the canonical ``persatrix-session`` wire key.
        """
        metadata = (
            ((SESSION_METADATA_GRPC_KEY, session_id),) if session_id is not None else ()
        )
        ack = await self._stub.ReceiveChannelMessage(event, metadata=metadata)
        # The handler enqueues fire-and-forget and acks immediately; assert the
        # wake was accepted, then wait for the detached processing task so the
        # interaction state is settled before the test inspects it.
        assert ack.success, f"ReceiveChannelMessage rejected: {ack.error_message!r}"
        await self._drain_inbound()

    async def _drain_inbound(self) -> None:
        # The no-loop fallback anchors each in-flight processing coroutine in
        # ``_inbound_fallback_tasks`` (strong ref). Awaiting the snapshot drains
        # the ingest; the done-callback empties the set. A bound keeps a stuck
        # task from hanging the whole pytest deadline.
        deadline = asyncio.get_running_loop().time() + 10.0
        while self._dispatcher._inbound_fallback_tasks:
            pending = list(self._dispatcher._inbound_fallback_tasks)
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=10.0,
            )
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("inbound fallback tasks did not drain")

    async def close_dm(self, peer: str) -> None:
        """Structurally close ``peer``'s DM and finalise the episode.

        The wire ``ChannelMessageEvent`` proto carries no ``chat_end`` flag, so
        the close is driven directly.  This is plumbing, not the property under
        test: the close path tags the episode with the interaction's *frozen*
        (open-time) session — set from the gRPC header on the first turn — not
        with whatever scope is bound at close time (the sibling-mislabel guard
        in ``persona_runtime/close_path.py``).
        """
        await self.agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "wrapping up",
                "channel_type": "dm",
                "respond_policy": "always",
                "mentions": [],
                "thread_parent_sender_id": "",
            },
            channel_id=_dm_channel(peer),
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await self.agent.drain_pending_summaries()


@pytest.fixture
async def grpc_world() -> AsyncIterator[_GrpcWorld]:
    agent = await _make_agent()
    dispatcher = EventDispatcher(agents={_AGENT_ID: agent})
    servicer = AgentServiceServicer({_AGENT_ID: agent}, dispatcher=dispatcher)

    server = grpc.aio.server()
    task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = task_pb2_grpc.AgentServiceStub(channel)
    try:
        yield _GrpcWorld(agent, dispatcher, stub)
    finally:
        # gRPC server stops first so no inbound can land mid-teardown, then the
        # client channel, then the agent's memory closes last.
        await server.stop(grace=0)
        await channel.close()
        await agent.close_memory()


# ─── Wire pin: the header freezes the interaction's session ─────


class TestSessionHeaderBindsInteraction:
    """A ``persatrix-session`` header drives the frozen interaction session."""

    async def test_header_freezes_interaction_session(
        self, grpc_world: _GrpcWorld,
    ) -> None:
        sid = "sess-ember-dm-alice-0001"
        await grpc_world.receive(
            _dm_event("alice", "hello from alice", message_id="m-a1"),
            session_id=sid,
        )

        scope = scope_for_dm(_AGENT_ID, "alice")
        interaction = grpc_world.agent._interaction_tracker.get(scope)
        assert interaction is not None, (
            "DM ingest did not open an interaction for the alice scope"
        )
        assert interaction.session_id == sid, (
            "the persatrix-session header must be lifted off the gRPC context, "
            "threaded onto the event envelope, and frozen as the interaction's "
            f"session; got {interaction.session_id!r}, expected {sid!r}"
        )

    async def test_no_header_falls_through_to_legacy(
        self, grpc_world: _GrpcWorld,
    ) -> None:
        """Negative control — without emission, the pre-activation behaviour.

        This is exactly the dormant state ISSUE-0082 describes: the rail is
        armed but unfed, so the handler falls back to its construction-time
        snapshot (``legacy`` here, env unset).  It proves the isolation in the
        sibling test is caused by the header, not by some ambient default.
        """
        await grpc_world.receive(
            _dm_event("alice", "hello from alice", message_id="m-a1"),
            session_id=None,
        )

        scope = scope_for_dm(_AGENT_ID, "alice")
        interaction = grpc_world.agent._interaction_tracker.get(scope)
        assert interaction is not None
        assert interaction.session_id == LEGACY_SESSION_ID, (
            "with no persatrix-session header the interaction must fall through "
            f"to the construction snapshot ({LEGACY_SESSION_ID!r}); got "
            f"{interaction.session_id!r}"
        )


# ─── Acceptance gate: concurrent-conversation recall isolation ──


class TestConcurrentConversationIsolation:
    """Two conversations for one agent recall in isolation; legacy stays shared."""

    async def test_two_conversations_isolated_legacy_visible_to_both(
        self, grpc_world: _GrpcWorld,
    ) -> None:
        agent = grpc_world.agent
        sid_alice = "sess-ember-dm-alice-0001"
        sid_bob = "sess-ember-dm-bob-0002"
        scope_alice = scope_for_dm(_AGENT_ID, "alice")
        scope_bob = scope_for_dm(_AGENT_ID, "bob")

        # A pre-activation row, tagged ``legacy`` (every pre-RFC write defaults
        # to this) — the carve-out that must survive activation for both
        # conversations so no historical memory is stranded.
        await agent._episodic_memory.store_episode(
            _LEGACY_MARKER, {}, session_id=LEGACY_SESSION_ID,
        )

        # Two concurrent conversations for the SAME agent: alice's DM under
        # session A, bob's DM under session B — the orchestrator emits a
        # distinct id per (agent, channel, user) triple.  Each is opened over
        # the real gRPC wire with its own header, then structurally closed so
        # the episode persists under the frozen session.
        await grpc_world.receive(
            _dm_event("alice", "alice's private note", message_id="m-a1"),
            session_id=sid_alice,
        )
        await grpc_world.receive(
            _dm_event("bob", "bob's private note", message_id="m-b1"),
            session_id=sid_bob,
        )
        await grpc_world.close_dm("alice")
        await grpc_world.close_dm("bob")

        # Recall under session A (the §D default path resolves the active
        # task-local scope) sees alice's episode + the legacy row, never bob's.
        with session_scope(sid_alice):
            alice_recall = await agent._episodic_memory.recall("", limit=50)
        alice_scopes = {e.scope for e in alice_recall}
        alice_summaries = [e.summary for e in alice_recall]
        assert scope_alice in alice_scopes, (
            "session A must recall its own conversation's episode"
        )
        assert scope_bob not in alice_scopes, (
            "cross-conversation bleed: session A recalled session B's episode"
        )
        assert any(_LEGACY_MARKER in s for s in alice_summaries), (
            "legacy carve-out: the pre-activation row must stay visible to A"
        )

        # Symmetric for session B.
        with session_scope(sid_bob):
            bob_recall = await agent._episodic_memory.recall("", limit=50)
        bob_scopes = {e.scope for e in bob_recall}
        bob_summaries = [e.summary for e in bob_recall]
        assert scope_bob in bob_scopes, (
            "session B must recall its own conversation's episode"
        )
        assert scope_alice not in bob_scopes, (
            "cross-conversation bleed: session B recalled session A's episode"
        )
        assert any(_LEGACY_MARKER in s for s in bob_summaries), (
            "legacy carve-out: the pre-activation row must stay visible to B"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
