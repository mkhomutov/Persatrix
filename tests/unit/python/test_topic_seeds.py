"""Tests for the topic-subject capture + recall-seeding path
(RFC 0026 topic-predicate amendment — RFC 0049 Phase 1 PR 1).

Three surfaces:

* :meth:`agents.memory.facts.FactStore.topic_subjects` — the bounded
  distinct-subject enumeration over live ``topic.*`` rows that feeds
  recall seeding.  Scoped identically to :meth:`FactStore.recall`
  (agent / session §D default / principal / epoch) so PR 1 widens
  *capture* without pre-widening *scope* — the cross-room L2 widening
  is RFC 0049 PR 2, not this PR.
* :func:`agents.persona_runtime.topic_seeds.match_topic_subjects` —
  the pure, deterministic stimulus-matching half (word-boundary,
  canonical-folded, capped).
* :func:`agents.persona_runtime.facts_section.recall_facts_for_event`
  with ``stimulus=`` — the wired seed-widening, including the
  preserved sender-less short-circuit (the PR-5 empty-context cost
  guard must not regress) and RFC 0037 §C stamping inheritance on
  extracted topic tuples.
"""

from __future__ import annotations

import pytest

from agents.memory.facts import FactStore
from agents.persona_runtime.facts_section import recall_facts_for_event
from agents.persona_runtime.topic_seeds import (
    TOPIC_SEED_LIMIT,
    match_topic_subjects,
    topic_subject_seeds,
)
from agents.persona_types import AgentEvent, EventType

# Applied per async class rather than module-wide: the pure
# ``match_topic_subjects`` tests are sync, and a module-level mark
# warns on every one of them (and would fail under -W error).
_asyncio = pytest.mark.asyncio


# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _seed_topic_fact(
    store: FactStore,
    *,
    subject: str = "atlas",
    predicate: str = "topic.has_deadline",
    object_: str = "friday",
    asserted_at: float = 1000.0,
    **kwargs,
) -> str:
    return await store.store(
        subject=subject,
        predicate=predicate,
        object=object_,
        source_interaction_id="int-1",
        asserted_at=asserted_at,
        **kwargs,
    )


def _channel_event(content: str, sender: str = "bob") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content},
        channel_id="group:standup",
        sender_id=sender,
    )


# ─── FactStore.topic_subjects ───────────────────────────────


@_asyncio
class TestTopicSubjectsQuery:
    async def test_returns_topic_subject(self, fact_store: FactStore):
        await _seed_topic_fact(fact_store)
        assert await fact_store.topic_subjects() == ["atlas"]

    async def test_excludes_person_subjects(self, fact_store: FactStore):
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="int-1", asserted_at=1000.0,
        )
        assert await fact_store.topic_subjects() == []

    async def test_distinct_across_predicates(self, fact_store: FactStore):
        await _seed_topic_fact(fact_store, predicate="topic.has_deadline")
        await _seed_topic_fact(
            fact_store, predicate="topic.owned_by", object_="bob",
            asserted_at=1001.0,
        )
        assert await fact_store.topic_subjects() == ["atlas"]

    async def test_supersede_chain_keeps_subject_live(
        self, fact_store: FactStore,
    ):
        """A superseding assertion keeps the subject seedable — the
        successor row is live."""
        await _seed_topic_fact(fact_store, asserted_at=1000.0)
        await _seed_topic_fact(
            fact_store, object_="monday", asserted_at=2000.0,
        )
        assert await fact_store.topic_subjects() == ["atlas"]

    async def test_excludes_fully_superseded_subject(
        self, fact_store: FactStore,
    ):
        """A topic whose every row is superseded no longer seeds.

        Load-bearing on the ``superseded_by IS NULL`` clause: the
        supersede-chain case above passes with or without it (the
        successor is live), so retraction is what actually pins it."""
        fact_id = await _seed_topic_fact(fact_store, asserted_at=1000.0)
        successor = await fact_store.store(
            subject="atlas", predicate="topic.decided", object="shelved",
            source_interaction_id="int-2", asserted_at=2000.0,
        )
        await fact_store.supersede(fact_id, successor)
        await fact_store.supersede(successor, fact_id)
        assert await fact_store.topic_subjects() == []

    async def test_foreign_session_not_enumerated(
        self, fact_store: FactStore,
    ):
        """Scope discipline: ``topic_subjects`` carries the same §D
        session filter as ``recall`` — a topic asserted under another
        named session does not seed."""
        await _seed_topic_fact(fact_store, session_id="other-session")
        assert await fact_store.topic_subjects() == []

    async def test_limit_keeps_most_recent(self, fact_store: FactStore):
        """LIMIT applies after the recency ORDER BY — the newest
        subjects survive the cap, not an arbitrary two."""
        for i in range(5):
            await _seed_topic_fact(
                fact_store, subject=f"project {i}",
                asserted_at=1000.0 + i,
            )
        assert await fact_store.topic_subjects(limit=2) == [
            "project 4", "project 3",
        ]

    async def test_most_recent_first_deterministic(
        self, fact_store: FactStore,
    ):
        await _seed_topic_fact(
            fact_store, subject="atlas", asserted_at=1000.0,
        )
        await _seed_topic_fact(
            fact_store, subject="borealis", asserted_at=2000.0,
        )
        assert await fact_store.topic_subjects() == ["borealis", "atlas"]

    async def test_limit_clamps(self, fact_store: FactStore):
        for i in range(5):
            await _seed_topic_fact(
                fact_store, subject=f"project {i}",
                asserted_at=1000.0 + i,
            )
        assert len(await fact_store.topic_subjects(limit=2)) == 2

    async def test_agent_isolation(self, fact_store: FactStore):
        """RFC 0008 §H — another agent's topic rows never seed."""
        other = FactStore(agent_id="other-agent", shared_db=fact_store._db)
        await other.initialize()
        await _seed_topic_fact(other)
        assert await fact_store.topic_subjects() == []


# ─── match_topic_subjects (pure) ────────────────────────────


class TestMatchTopicSubjects:
    def test_matches_mentioned_subject(self):
        assert match_topic_subjects(
            "How is Atlas coming along?", ["atlas"],
        ) == ["atlas"]

    def test_word_boundary_no_substring_bleed(self):
        """``atlas`` must not match inside ``atlases`` — over-seeding
        burns recall round-trips and budget on unrelated topics."""
        assert match_topic_subjects(
            "I collect atlases as a hobby", ["atlas"],
        ) == []

    def test_case_and_whitespace_folded(self):
        assert match_topic_subjects(
            "the  Q3   ROADMAP slipped", ["q3 roadmap"],
        ) == ["q3 roadmap"]

    def test_unmentioned_subject_not_seeded(self):
        assert match_topic_subjects(
            "lunch plans anyone?", ["atlas"],
        ) == []

    def test_cap_respected(self):
        subjects = [f"proj{i}" for i in range(10)]
        stimulus = " ".join(subjects)
        assert (
            len(match_topic_subjects(stimulus, subjects))
            == TOPIC_SEED_LIMIT
        )

    def test_exclude_dedupes_person_seeds(self):
        """A topic subject colliding with the sender / self seed is not
        seeded twice."""
        assert match_topic_subjects(
            "ask bob about atlas", ["bob", "atlas"], exclude={"bob"},
        ) == ["atlas"]

    def test_order_follows_subject_list(self):
        """Deterministic: matches keep the store's most-recent-first
        subject order, not stimulus position."""
        assert match_topic_subjects(
            "atlas depends on borealis", ["borealis", "atlas"],
        ) == ["borealis", "atlas"]

    def test_empty_stimulus_no_matches(self):
        assert match_topic_subjects("", ["atlas"]) == []

    def test_non_str_stimulus_no_crash(self):
        """A bridge may pass a non-str ``content`` through
        (``channel_ingest`` tolerates malformed wire values); this runs
        outside the tier's try-block, so a raise here would fail the
        whole turn."""
        assert match_topic_subjects(123, ["atlas"]) == []  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("subject", "stimulus"),
        [
            ("c++ migration", "the c++ migration is done"),
            ("atlas (v2)", "status of atlas (v2)?"),
            ("q3/q4 plan", "see the q3/q4 plan"),
        ],
    )
    def test_regex_metacharacter_subjects_match(
        self, subject: str, stimulus: str,
    ):
        """Subjects carrying regex metacharacters are escaped, and the
        lookaround anchors (not ``\\b``) are what make subjects that
        start or end with a non-word character matchable at all — with
        ``\\b`` flanks, ``atlas (v2)`` and ``c++`` never match."""
        assert match_topic_subjects(stimulus, [subject]) == [subject]

    @pytest.mark.parametrize("subject", ["the", "you", "and"])
    def test_stopword_subjects_never_seed(self, subject: str):
        """A function-word subject matches nearly every message, and
        recency ordering hands the newest writer the seed slots — one
        induced ``the`` tuple would otherwise occupy every slot on
        every subsequent turn."""
        assert match_topic_subjects(
            "can you send me the report and the notes", [subject],
        ) == []

    def test_short_subjects_never_seed(self):
        assert match_topic_subjects("ok ai v2 go", ["ai", "v2"]) == []


# ─── topic_subject_seeds (store-backed) ─────────────────────


@_asyncio
class TestTopicSubjectSeeds:
    async def test_seeds_from_store(self, fact_store: FactStore):
        await _seed_topic_fact(fact_store)
        assert await topic_subject_seeds(
            fact_store, "any news on atlas?", exclude=set(),
        ) == ["atlas"]

    async def test_none_store_returns_empty(self):
        assert await topic_subject_seeds(
            None, "any news on atlas?", exclude=set(),
        ) == []

    async def test_backend_failure_returns_empty(self):
        class _Boom:
            agent_id = "test-agent"

            async def topic_subjects(self, *, limit):
                raise RuntimeError("db gone")

        assert await topic_subject_seeds(
            _Boom(), "any news on atlas?", exclude=set(),
        ) == []


# ─── recall_facts_for_event wiring ──────────────────────────


@_asyncio
class TestRecallFactsTopicSeeding:
    async def test_topic_fact_recalled_when_stimulus_mentions_it(
        self, fact_store: FactStore,
    ):
        """The scenario-2 capture core: a stored topic fact is
        retrievable when the inbound stimulus names the topic."""
        await _seed_topic_fact(fact_store)
        facts = await recall_facts_for_event(
            fact_store,
            _channel_event("What's the status of Atlas?"),
            stimulus="What's the status of Atlas?",
        )
        assert [(f.subject, f.predicate, f.object) for f in facts] == [
            ("atlas", "topic.has_deadline", "friday"),
        ]

    async def test_no_stimulus_keeps_person_only_seeds(
        self, fact_store: FactStore,
    ):
        await _seed_topic_fact(fact_store)
        facts = await recall_facts_for_event(
            fact_store, _channel_event("What's the status of Atlas?"),
        )
        assert facts == []

    async def test_topic_seed_reads_only_topic_rows(
        self, fact_store: FactStore,
    ):
        """A ``topic.*`` tuple naming a PERSON must not turn that
        person into a general fact-read key.

        Without predicate-scoped seeding, one induced
        ``("bob", "topic.owned_by", …)`` makes the string ``bob`` a
        permanent seed: any later message mentioning Bob — from any
        sender — pulls every private fact ever stored about him into
        that turn's prompt.  The person-seed path is deliberately
        unscoped (a counterparty's own turn should surface everything
        about them); the topic path is not."""
        await fact_store.store(
            subject="bob", predicate="topic.owned_by", object="atlas",
            source_interaction_id="int-1", asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob", predicate="lives_in", object="dortmund",
            source_interaction_id="int-1", asserted_at=1000.0,
        )
        facts = await recall_facts_for_event(
            fact_store,
            _channel_event("anything I should know about bob?", "mallory"),
            stimulus="anything I should know about bob?",
        )
        assert [f.predicate for f in facts] == ["topic.owned_by"]

    async def test_person_seed_still_reads_every_predicate(
        self, fact_store: FactStore,
    ):
        """The scoping is topic-seed-local — when Bob is the sender,
        his own facts still surface in full."""
        await fact_store.store(
            subject="bob", predicate="lives_in", object="dortmund",
            source_interaction_id="int-1", asserted_at=1000.0,
        )
        facts = await recall_facts_for_event(
            fact_store, _channel_event("hello again", "bob"),
        )
        assert [f.predicate for f in facts] == ["lives_in"]

    async def test_person_seeds_precede_topic_seeds(
        self, fact_store: FactStore,
    ):
        """Seed order is load-bearing: ``render_facts_section`` drains
        the per-tier slice sequentially in seed order, so topic blocks
        arriving first would crowd out ``self.*`` rows under a tight
        budget — the M-2 persona-inversion hazard."""
        await fact_store.store(
            subject="self", predicate="self.holds_value", object="candour",
            source_interaction_id="int-1", asserted_at=1000.0,
        )
        await _seed_topic_fact(fact_store, asserted_at=2000.0)
        facts = await recall_facts_for_event(
            fact_store,
            _channel_event("where are we on atlas?"),
            stimulus="where are we on atlas?",
        )
        assert [f.subject for f in facts] == ["self", "atlas"]

    async def test_senderless_event_short_circuits(
        self, fact_store: FactStore,
    ):
        """The PR-5 empty-context cost guard survives the widening: a
        sender-less TICK issues no DB round-trip even with a stimulus."""
        calls: list[str] = []
        original = fact_store.topic_subjects

        async def _spy(*, limit):
            calls.append("topic_subjects")
            return await original(limit=limit)

        fact_store.topic_subjects = _spy  # type: ignore[assignment, method-assign]
        tick = AgentEvent(event_type=EventType.TICK, payload={})
        facts = await recall_facts_for_event(
            fact_store, tick, stimulus="atlas atlas atlas",
        )
        assert facts == []
        assert calls == []


# ─── RFC 0037 §C stamping inheritance ───────────────────────


@_asyncio
class TestTopicFactStampingInheritance:
    async def test_extracted_topic_tuple_inherits_protection(
        self, fact_store: FactStore,
    ):
        """A topic fact extracted from a ``restricted`` interaction is
        stamped ``restricted`` — same §C inheritance as person facts
        (the 0049-pr-plan PR 1 acceptance line)."""
        from agents.persona_runtime.fact_extractor import (
            store_extracted_facts,
        )

        stored = await store_extracted_facts(
            fact_store,
            facts=[{
                "subject": "Atlas",
                "predicate": "topic.has_deadline",
                "object": "friday",
                "certainty": 0.9,
            }],
            source_interaction_id="int-dm",
            asserted_at=1000.0,
            session_id="sess-1",
            sender_id="bob",
            protection_level="restricted",
            source_channel_id="dm:alice:bob",
        )
        assert stored == 1
        rows = await fact_store.recall(
            subject="atlas", sessions=["sess-1"],
        )
        assert len(rows) == 1
        assert rows[0].protection_level == "restricted"
        assert rows[0].source_channel_id == "dm:alice:bob"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
