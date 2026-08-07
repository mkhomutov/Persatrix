"""ISSUE-0082 Part 2 PR 2 — Go→Python principal-emission wire proof + tenant gate.

The acceptance gate for the v0.3.14 tenant boundary, and the sibling of
:file:`test_session_emission_isolation.py` one axis over.

ISSUE-0081 built the whole persona-side principal vertical in 2026-05-29: a
``principal_id`` dimension on all five memory tables (migration v11), recall
predicates that are **strict equality with no carve-out**, a
``principal_scope`` ContextVar, and the header lift in the servicer.  v0.3.13
PR 1 threaded the axis across the executor hop.  Every bit of it resolved to
the single-tenant ``'local'`` default, because the orchestrator emitted
nothing.  v0.3.14 PR 1 landed the Go rail dormant; PR 2 — the change this file
gates — supplies the **source**: the RFC 0039 §F verified ``participant_id``,
threaded onto the request context at the one place identity is resolved and
emitted at the dispatch chokepoint.

Two layers of pin, the same decomposition the epoch axis uses
(:file:`test_epoch_run_isolation.py`) and the session axis before it:

* :class:`TestPrincipalHeaderBindsHandlerScope` — **the wire pin**, over a
  real ephemeral gRPC server.  A ``persatrix-principal`` header on a real
  ``ReceiveChannelMessage`` call is lifted by the servicer, threaded onto the
  event envelope, and bound as the task-local ``principal_scope`` for the
  handler's lifetime — observed from *inside* the turn, at the moment the LLM
  is called, which is where the recall and write seams run.  Its negative
  control (no header → the ``'local'`` default) is what proves the header is
  doing the work rather than some ambient value.

* :class:`TestTwoPrincipalsIsolatedInOneProcess` — **the tenant gate**: two
  principals, one agent, one process, one shared room, and no bleed across the
  cross-room tiers that the release's threat model names (travelling facts and
  the participant-keyed relationship/identity record, whose primary key omits
  ``session_id`` and which therefore carried across accounts before this
  release).  Driven through ``principal_scope`` at the tier layer, because the
  wire→scope link is layer 1's job and re-proving it per tier would only pin
  the same seam five more times.

  :class:`TestActivationDayReset` pins the accepted, *stated* cost of the same
  strictness: rows written before emission existed carry ``'local'`` and are
  unreachable for an authenticated principal.  It is asserted here so the
  release note's claim is executable rather than editorial — bridging it would
  BE the cross-tenant bridge the boundary forbids.

The Go-side emission is pinned independently
(``internal/channels/grpc_dispatcher_principal_test.go`` for the rail,
``internal/server/principal_producer_test.go`` for the producer); standing up
the real Go binary from pytest is heavier scaffolding than a contract pin
justifies — the same reasoning as
:file:`test_channel_cascade_backstop_cross_process.py`.  The header *string*
comes from :data:`agents.principal_id.PRINCIPAL_METADATA_GRPC_KEY`, the same
constant the servicer lifts, so this emit cannot drift to a stale key; the
cross-language agreement on the literal ``"persatrix-principal"`` is pinned
separately on both sides (``tests/unit/python/test_principal_id_leaf_module.py``,
``internal/observability/grpcmeta/grpcmeta_test.go``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.facade import MemoryStore
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.principal_id import (
    DEFAULT_PRINCIPAL_ID,
    PRINCIPAL_METADATA_GRPC_KEY,
    current_principal_id,
    principal_scope,
)
from agents.server_servicers import AgentServiceServicer
from agents.tools.registry import clear_registry

_AGENT_ID = "ember-owl"

#: The two authenticated principals.  These are RFC 0039 §A ``participant_id``
#: values — one per account, 1:1 with a ``UserParticipant`` — not account ids,
#: so an account rename never re-partitions anyone's memory.
_ALICE = "alice-participant"
_BOB = "bob-participant"

#: The ONE room both principals speak in.  Holding the channel constant is
#: what makes this a tenant gate rather than a second room-isolation test: the
#: session axis is identical for both speakers, so nothing but the principal
#: can be what separates them.  It is also the shared-room semantics the
#: release notes must state — two authenticated people in one channel get
#: per-speaker persona memory.
_ROOM = "group:planning"

#: The subject both principals hold private facts about.
_SUBJECT = "the-vendor"

_TS = "2026-08-06T12:00:00Z"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _no_ambient_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the construction-time principal snapshot to the ``'local'`` default.

    Every tier reads ``PERSATRIX_PRINCIPAL_ID`` once at construction into
    ``_active_principal_id``; an exported value would make the "no header"
    negative control land under *that* value instead of ``'local'``, masking
    the fall-through it exists to prove.
    """
    monkeypatch.delenv("PERSATRIX_PRINCIPAL_ID", raising=False)


# ─── Layer 1: the wire pin ──────────────────────────────────────


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "ISSUE-0082 Part 2 principal-emission gate",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the v0.3.14 principal-emission gate.",
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
        "memory": {"db_path": ":memory:", "working": {"max_tokens": 50000}},
        "relationships": [],
    }


class _ScopeSpy:
    """Records the task-local principal bound at LLM-call time.

    The LLM call happens *inside* ``on_event``'s scope binding, on the same
    task as the recall and write seams — so what this observes is exactly what
    those seams would resolve.  Observing there rather than asserting on the
    event envelope is deliberate: the envelope only shows the header arrived,
    not that it was bound.
    """

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    def client(self) -> LLMClient:
        provider = AsyncMock()

        async def _create_message(*_args, **_kwargs) -> LLMResponse:
            self.seen.append(current_principal_id())
            return LLMResponse(
                text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                stop_reason=StopReason.END_TURN,
                usage=Usage(10, 5),
            )

        provider.create_message = AsyncMock(side_effect=_create_message)
        provider.format_tool_definitions = MagicMock(return_value=[])
        provider.append_tool_round = MagicMock(
            side_effect=lambda msgs, resp, results: msgs,
        )
        return LLMClient(provider)


class _GrpcWorld:
    """A real ``AgentServiceServicer`` + client over an ephemeral gRPC port.

    :meth:`receive` emits ``persatrix-principal`` exactly as the
    orchestrator's ``grpcmeta.InjectPrincipal`` does on its outbound dispatch,
    then drains the fire-and-forget ingest so the turn is complete on return.
    """

    def __init__(
        self,
        agent: _LLMPersonaAgent,
        dispatcher: EventDispatcher,
        stub: task_pb2_grpc.AgentServiceStub,
        spy: _ScopeSpy,
    ) -> None:
        self.agent = agent
        self.spy = spy
        self._dispatcher = dispatcher
        self._stub = stub

    async def receive(
        self, sender: str, content: str, *, principal: str | None, message_id: str,
    ) -> None:
        event = task_pb2.ChannelMessageEvent(
            message_id=message_id,
            channel_id=_ROOM,
            channel_type="group",
            sender_id=sender,
            content=content,
            mentions=[_AGENT_ID],  # clear the response gate deterministically
            timestamp=_TS,
            respond_policy="always",
            thread_parent_sender_id="",
            cascade_depth=0,
        )
        metadata = (
            ((PRINCIPAL_METADATA_GRPC_KEY, principal),) if principal is not None else ()
        )
        ack = await self._stub.ReceiveChannelMessage(event, metadata=metadata)
        assert ack.success, f"ReceiveChannelMessage rejected: {ack.error_message!r}"
        await self._drain_inbound()

    async def _drain_inbound(self) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        while self._dispatcher._inbound_fallback_tasks:
            pending = list(self._dispatcher._inbound_fallback_tasks)
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=10.0,
            )
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("inbound fallback tasks did not drain")


@pytest.fixture
async def grpc_world() -> AsyncIterator[_GrpcWorld]:
    spy = _ScopeSpy()
    agent = create_persona_agent(
        agent_id=_AGENT_ID,
        config=_persona_config(_AGENT_ID),
        llm_client=spy.client(),
    )
    assert isinstance(agent, _LLMPersonaAgent)
    await agent.initialize_memory()

    dispatcher = EventDispatcher(agents={_AGENT_ID: agent})
    servicer = AgentServiceServicer({_AGENT_ID: agent}, dispatcher=dispatcher)

    server = grpc.aio.server()
    task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = task_pb2_grpc.AgentServiceStub(channel)
    try:
        yield _GrpcWorld(agent, dispatcher, stub, spy)
    finally:
        await server.stop(grace=0)
        await channel.close()
        await agent.close_memory()


class TestPrincipalHeaderBindsHandlerScope:
    """A ``persatrix-principal`` header binds the handler's tenant scope."""

    async def test_header_binds_principal_scope(self, grpc_world: _GrpcWorld) -> None:
        await grpc_world.receive(
            "alice", "the vendor quoted us 40k", principal=_ALICE, message_id="m-a1",
        )
        assert grpc_world.spy.seen, "the turn never reached the LLM seam"
        assert all(p == _ALICE for p in grpc_world.spy.seen), (
            "the persatrix-principal header must be lifted off the gRPC context, "
            "threaded onto the event envelope, and bound as the task-local "
            f"principal_scope for the handler; got {grpc_world.spy.seen!r}"
        )

    async def test_no_header_falls_through_to_default(
        self, grpc_world: _GrpcWorld,
    ) -> None:
        """Negative control — the pre-activation shape, and the
        ``auth.mode: disabled`` / agent-origin contract.

        No header means no scope bound at all (``current_principal_id()`` is
        ``None``), so every seam falls through to its construction snapshot,
        which resolves to ``'local'``.  This is what makes the byte-level
        no-delta claim true rather than merely equal-valued.
        """
        await grpc_world.receive(
            "alice", "the vendor quoted us 40k", principal=None, message_id="m-a1",
        )
        assert grpc_world.spy.seen, "the turn never reached the LLM seam"
        assert all(p is None for p in grpc_world.spy.seen), (
            "with no persatrix-principal header no principal scope may be bound; "
            f"got {grpc_world.spy.seen!r}"
        )


# ─── Layer 2: the tenant gate ───────────────────────────────────


class _Tiers:
    """One agent's cross-room tiers over one shared DB.

    Built once and driven under different ``principal_scope``s, because that
    is how production works: the tiers snapshot their principal at
    construction (``'local'`` here, env unset) and the per-request ContextVar
    overrides it per turn.  A per-principal *rebuild* would prove far less —
    it could pass on construction snapshots alone, never exercising the
    override path the wire actually uses.
    """

    def __init__(
        self, facade: MemoryStore, rels: RelationshipMemory, facts: FactStore,
    ) -> None:
        self.facade = facade
        self.rels = rels
        self.facts = facts


@pytest.fixture
async def tiers(tmp_path: Path) -> AsyncIterator[_Tiers]:
    db = str(tmp_path / "shared.db")
    facade = MemoryStore(agent_id=_AGENT_ID, db_path=db)
    await facade.initialize()
    rels = RelationshipMemory(agent_id=_AGENT_ID, db_path=db)
    await rels.initialize()
    facts = FactStore(agent_id=_AGENT_ID, db_path=db)
    await facts.initialize()
    try:
        yield _Tiers(facade, rels, facts)
    finally:
        await facade.close()
        await rels.close()
        await facts.close()


async def _speak(t: _Tiers, principal: str, marker: str, asserted_at: float) -> None:
    """One principal's turn in the shared room, across every cross-room tier.

    Every write leaves the session axis at its default, identical for both
    principals — the same discipline :file:`test_epoch_run_isolation.py` uses
    (it pins ``PERSATRIX_SESSION_ID`` to one shared room for every run): hold
    every other axis constant so the principal is the only thing that could
    isolate them.  Only the ``principal_scope`` differs between calls.
    """
    with principal_scope(principal):
        await t.facade.store_observation(f"{marker}: discussed the vendor")
        await t.rels.update_trust("alice", 0.2, f"{marker}: kept a commitment")
        await t.rels.record_interaction("alice", "task_delegation", outcome=marker)
        await t.facts.store(
            subject=_SUBJECT, predicate="works_at", object=marker,
            source_interaction_id=f"ix-{marker}", asserted_at=asserted_at,
        )


class TestTwoPrincipalsIsolatedInOneProcess:
    """Two authenticated people, one agent, one room — no bleed either way."""

    async def test_neither_principal_recalls_the_other(self, tiers: _Tiers) -> None:
        await _speak(tiers, _ALICE, "alice-secret", 1000.0)
        await _speak(tiers, _BOB, "bob-secret", 2000.0)

        with principal_scope(_ALICE):
            episodes = await tiers.facade.retrieve_relevant("vendor")
            facts = await tiers.facts.recall(subject=_SUBJECT)
            summary = await tiers.rels.get_relationship_summary("alice")
        assert any("alice-secret" in e.content for e in episodes), (
            "a principal must recall its own room episodes"
        )
        assert not any("bob-secret" in e.content for e in episodes), (
            "cross-tenant bleed: alice recalled bob's episode from the shared room"
        )
        assert [f.object for f in facts] == ["alice-secret"], (
            "cross-tenant bleed: the travelling-fact tier crossed accounts"
        )
        assert summary.interaction_count == 1, (
            "cross-tenant bleed: the participant-keyed relationship record "
            "(primary key omits session_id) counted the other account's turn"
        )

        with principal_scope(_BOB):
            episodes = await tiers.facade.retrieve_relevant("vendor")
            facts = await tiers.facts.recall(subject=_SUBJECT)
            summary = await tiers.rels.get_relationship_summary("alice")
        assert any("bob-secret" in e.content for e in episodes)
        assert not any("alice-secret" in e.content for e in episodes), (
            "cross-tenant bleed: bob recalled alice's episode from the shared room"
        )
        assert [f.object for f in facts] == ["bob-secret"]
        assert summary.interaction_count == 1

    async def test_a_principal_reads_its_own_arc_back(self, tiers: _Tiers) -> None:
        """The complement: strictness must not narrow recall *within* a tenant.

        Without this, a filter that returned nothing at all would pass the
        isolation test above.
        """
        await _speak(tiers, _ALICE, "alice-first", 1000.0)
        await _speak(tiers, _ALICE, "alice-second", 2000.0)

        with principal_scope(_ALICE):
            episodes = await tiers.facade.retrieve_relevant("vendor")
            summary = await tiers.rels.get_relationship_summary("alice")
        contents = " ".join(e.content for e in episodes)
        assert "alice-first" in contents and "alice-second" in contents
        assert summary.interaction_count == 2


class TestActivationDayReset:
    """The accepted, stated cost of a carve-out-free axis.

    Migration v11 backfilled every pre-existing row to ``'local'`` and the
    predicate is strict equality, so on the day emission lands a deployment
    that has been running ``auth.mode: enabled`` since v0.3.12 finds each
    persona's accumulated memory unreachable — indistinguishable, to an
    operator, from "the persona forgot everything".

    The session axis absorbed its equivalent with a ``legacy`` carve-out; the
    principal axis cannot, because an always-visible principal IS the
    cross-tenant bridge this boundary forbids.  So the reset is taken as-is
    and made *executable* here, so the release note stating it can be checked
    rather than believed.
    """

    async def test_pre_activation_local_rows_are_unreachable(
        self, tiers: _Tiers,
    ) -> None:
        # The pre-activation corpus: written with no scope bound at all, so
        # every tier resolves its construction snapshot — 'local', exactly
        # what migration v11 backfilled.
        await _speak(tiers, DEFAULT_PRINCIPAL_ID, "pre-activation", 500.0)

        for principal in (_ALICE, _BOB):
            with principal_scope(principal):
                episodes = await tiers.facade.retrieve_relevant("vendor")
                facts = await tiers.facts.recall(subject=_SUBJECT)
                summary = await tiers.rels.get_relationship_summary("alice")
            assert not any("pre-activation" in e.content for e in episodes), (
                f"the 'local' corpus must not follow {principal} — an "
                "always-visible default principal would be the cross-tenant bridge"
            )
            assert facts == []
            assert summary.interaction_count == 0

        # ...and it is not destroyed, merely partitioned: the operator remedy
        # (run single-tenant, or re-tag the rows) reaches it under 'local'.
        with principal_scope(DEFAULT_PRINCIPAL_ID):
            episodes = await tiers.facade.retrieve_relevant("vendor")
        assert any("pre-activation" in e.content for e in episodes), (
            "the pre-activation corpus must remain intact under 'local' — "
            "the reset is a partition, not a deletion"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
