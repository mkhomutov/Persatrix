"""ISSUE-0165 — a persona reads its relationship with a peer the same way
in every session.

A persona keeps one relationship row per peer: the trust score, the notes
on the last trust change, and the peer's identity.  The row's primary key
has no session in it, because a relationship follows the peer across
channels (Decision 3 in ``docs/memory-scope-axes.md``).  The row still
records the session it was first written in, and until this fix every
read filtered on that tag.  So a peer whose row was first written in one
session read as a stranger in every other one:

* With ``PERSATRIX_SESSION_ID`` set at boot, a peer seeded from the
  ``relationships:`` config is tagged with that boot session.  Channel
  turns run under the orchestrator's per-channel session, so the
  configured trust never reached a prompt.
* A peer first met under one session was hidden in the next, together
  with that next session's own conversations with the peer.

The fix drops the session filter from the row read only.  The history
drawn from the ``interactions`` table (count, recent interactions, first
and last seen) stays per-session, as ISSUE-0080 set it.  The epoch and
principal filters stay strict.
"""

from __future__ import annotations

import copy
import time

import pytest

from agents.epoch_id import epoch_scope
from agents.memory.interactions import Interaction, Turn
from agents.memory.relationship import RelationshipMemory
from agents.persona import create_persona_agent
from agents.persona_runtime.record_close import record_closed_interaction
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import principal_scope
from agents.session_id import SESSION_ID_ENV_VAR, session_scope

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

#: Channel sessions, shaped like the UUIDv7 ids the orchestrator mints
#: per (agent, channel).
CHANNEL_SESSION = "0199a000-0000-7000-8000-000000000001"
OTHER_SESSION = "0199a000-0000-7000-8000-000000000002"

#: The ``relationships:`` block ``config/agents.yaml`` gives ember-owl.
_SEEDS: list[dict[str, object]] = [{"agent_id": "iron-fox", "trust_level": 0.9}]


@pytest.fixture
async def booted_with_session(monkeypatch: pytest.MonkeyPatch):
    """ember-owl's relationship tier, booted with ``PERSATRIX_SESSION_ID``
    set and seeded from config the way ``initialize_memory`` seeds it."""
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-boot")
    mem = RelationshipMemory(agent_id="ember-owl", db_path=":memory:")
    await mem.initialize(config_relationships=_SEEDS, session_id="run-boot")
    yield mem
    await mem.close()


@pytest.fixture
async def rel():
    mem = RelationshipMemory(agent_id="ember-owl", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestConfiguredPeerInEveryChannel:
    """The reported case: a seed tagged with the boot session."""

    async def test_seeded_trust_reads_the_same_inside_a_channel(
        self, booted_with_session: RelationshipMemory,
    ) -> None:
        assert await booted_with_session.get_trust("iron-fox") == pytest.approx(0.9)
        with session_scope(CHANNEL_SESSION):
            assert await booted_with_session.get_trust("iron-fox") == pytest.approx(0.9)
            summary = await booted_with_session.get_relationship_summary("iron-fox")
        assert summary.trust_score == pytest.approx(0.9)

    async def test_the_channels_own_interactions_are_counted(
        self, booted_with_session: RelationshipMemory,
    ) -> None:
        await booted_with_session.record_interaction(
            "iron-fox", "conversation", outcome="planned the release",
            session_id=CHANNEL_SESSION,
        )
        with session_scope(CHANNEL_SESSION):
            summary = await booted_with_session.get_relationship_summary("iron-fox")
        assert summary.trust_score == pytest.approx(0.9)
        assert summary.interaction_count == 1
        assert [i.outcome for i in summary.recent_interactions] == [
            "planned the release",
        ]
        assert summary.last_interaction_at is not None

    async def test_the_list_read_shows_the_seed_inside_a_channel(
        self, booted_with_session: RelationshipMemory,
    ) -> None:
        with session_scope(CHANNEL_SESSION):
            rels = await booted_with_session.get_all_relationships()
        assert [r.other_participant_id for r in rels] == ["iron-fox"]
        assert rels[0].trust_score == pytest.approx(0.9)
        assert rels[0].interaction_count == 0


class TestPeerFirstMetInAnotherSession:
    """The wider case: the row was first written in another session."""

    async def test_a_later_session_sees_its_own_history_only(
        self, rel: RelationshipMemory,
    ) -> None:
        for session, outcome in (
            (CHANNEL_SESSION, "met at the lake"),
            (OTHER_SESSION, "talked about the ferry"),
            (OTHER_SESSION, "talked about the pier"),
        ):
            await rel.record_interaction(
                "user-alice", "conversation", outcome=outcome,
                other_participant_type="user", session_id=session,
            )
        with session_scope(OTHER_SESSION):
            later = await rel.get_relationship_summary(
                "user-alice", other_participant_type="user",
            )
        with session_scope(CHANNEL_SESSION):
            first = await rel.get_relationship_summary(
                "user-alice", other_participant_type="user",
            )
        assert later.interaction_count == 2
        assert {i.outcome for i in later.recent_interactions} == {
            "talked about the ferry", "talked about the pier",
        }
        assert first.interaction_count == 1
        assert [i.outcome for i in first.recent_interactions] == ["met at the lake"]

    async def test_trust_is_one_value_for_the_pair(
        self, rel: RelationshipMemory,
    ) -> None:
        await rel.record_interaction("peer-b", "conversation", session_id=CHANNEL_SESSION)
        await rel.update_trust("peer-b", 0.2, "kept a promise")
        readings: list[float] = []
        for session in (CHANNEL_SESSION, OTHER_SESSION, "legacy"):
            with session_scope(session):
                readings.append(await rel.get_trust("peer-b"))
                summary = await rel.get_relationship_summary("peer-b")
                readings.append(summary.trust_score)
                assert summary.notes == "kept a promise"
        assert readings == pytest.approx([0.7] * 6)


class TestSeededRowStaysInsideItsEpochAndTenant:
    """Only the session axis came off the row read.

    The seed is tagged with a boot session here, not ``legacy``, so the
    reads inside ``session_scope`` really are foreign-session reads: they
    find the row (that is ISSUE-0165) while a foreign epoch or tenant
    still finds nothing.  These are also the only tests that a config
    seed is tagged with the *active* epoch and principal rather than the
    column defaults.

    Each read that the prompt path uses is checked, ``get_relationship_summary``
    above all: it is the one row read a persona turn makes, and since
    ISSUE-0165 the epoch and principal predicates are all that bind it.
    """

    async def test_another_epoch_reads_neutral_trust(self) -> None:
        mem = RelationshipMemory(agent_id="ember-owl", db_path=":memory:")
        try:
            with epoch_scope("run-1"):
                await mem.initialize(
                    config_relationships=_SEEDS, session_id="run-boot",
                )
            with epoch_scope("run-2"), session_scope(CHANNEL_SESSION):
                assert await mem.get_trust("iron-fox") == 0.5
                foreign = await mem.get_relationship_summary("iron-fox")
                assert foreign.trust_score == 0.5
                assert foreign.notes is None
                assert (await mem.get_all_relationships()) == []
            with epoch_scope("run-1"), session_scope(CHANNEL_SESSION):
                assert await mem.get_trust("iron-fox") == pytest.approx(0.9)
                own = await mem.get_relationship_summary("iron-fox")
                assert own.trust_score == pytest.approx(0.9)
        finally:
            await mem.close()

    async def test_another_principal_reads_neutral_trust(self) -> None:
        mem = RelationshipMemory(agent_id="ember-owl", db_path=":memory:")
        try:
            with principal_scope("tenant-a"):
                await mem.initialize(
                    config_relationships=_SEEDS, session_id="run-boot",
                )
                await mem.update_trust("iron-fox", -0.2, "missed the handover")
            with principal_scope("tenant-b"), session_scope(CHANNEL_SESSION):
                assert await mem.get_trust("iron-fox") == 0.5
                foreign = await mem.get_relationship_summary("iron-fox")
                assert foreign.trust_score == 0.5
                # tenant-a's trust note must not cross the tenant wall.
                assert foreign.notes is None
                assert (await mem.get_all_relationships()) == []
            with principal_scope("tenant-a"), session_scope(CHANNEL_SESSION):
                assert await mem.get_trust("iron-fox") == pytest.approx(0.7)
                own = await mem.get_relationship_summary("iron-fox")
                assert own.trust_score == pytest.approx(0.7)
                assert own.notes == "missed the handover"
        finally:
            await mem.close()


class TestConfiguredPeerReachesThePrompt:
    """End to end through the persona: boot with the variable set, close a
    DM with the configured peer in its channel, then take the next turn."""

    async def test_relationship_section_renders_in_the_dm(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-boot")
        config = copy.deepcopy(_PERSONA_CONFIG)
        config["relationships"] = copy.deepcopy(_SEEDS)
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        try:
            now = time.time()
            # What the close path hands over: the interaction keeps the
            # session it was opened under, the DM channel's.
            closed = Interaction(
                interaction_id="ix-1", scope="dm:ember-owl:iron-fox",
                started_at=now, closed_at=now, session_id=CHANNEL_SESSION,
                turns=[Turn(at=now, payload={"participant_type": "agent"})],
            )
            await record_closed_interaction(
                agent.memory, "ember-owl", closed, "planned the release",
                False, session_id=CHANNEL_SESSION,
            )
            with session_scope(CHANNEL_SESSION):
                await agent._inject_memory_context(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": "morning"},
                    sender_id="iron-fox",
                    metadata={"sender_participant_type": "agent"},
                ))
            section = agent._working_memory.get_section("relationship_context")
            assert section is not None, "the configured peer read as a stranger"
            assert "Relationship with iron-fox" in section.content
            assert "Trust: 0.90" in section.content
            assert "Interactions: 1" in section.content
        finally:
            await agent.close_memory()
