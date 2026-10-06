"""Facts one conversation taught together, from the extractor to the prompt
(ISSUE-0181).

A briefing that sets three constraints yields three facts under one
subject and predicate.  The extractor stores them one after another with
one source interaction and one ``asserted_at``, and each used to
supersede the one before it, so the persona kept only the last.

:mod:`test_fact_store_written_together` owns the storage rule under each
write ordering.  This file owns what the rule means end to end: what
``store_extracted_facts`` leaves live, what the facts section of the
prompt then prints, and what the audit trail records.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agents.memory.facts import FactStore
from agents.persona_runtime.fact_extractor import store_extracted_facts
from agents.persona_runtime.facts_section import (
    recall_facts_for_event,
    render_facts_section,
)
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_types import AgentEvent, EventType

from ._fact_audit_test_helpers import event_dict

_TOPIC = "event planning"
_BRIEFING = [
    "Saturdays in May are unavailable",
    "ticket price ceiling is $25",
    "lighting crew limited to two people",
]


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB."""
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _extract(
    store: FactStore,
    objects: list[str],
    *,
    source: str = "ix-1",
    at: float = 1000.0,
) -> int:
    """Run one extraction's worth of ``topic.decided`` tuples through the
    extractor's store path; return how many it stored."""
    return await store_extracted_facts(
        store,
        facts=[
            {"subject": _TOPIC, "predicate": "topic.decided", "object": obj}
            for obj in objects
        ],
        source_interaction_id=source,
        asserted_at=at,
        session_id="legacy",
    )


def _supersede_events(
    caplog: pytest.LogCaptureFixture,
) -> list[dict[str, Any]]:
    """The ``fact.supersede`` audit events captured so far."""
    events = [event_dict(rec) for rec in caplog.records]
    return [e for e in events if e.get("event") == "fact.supersede"]


class TestOneCloseKeepsEveryFact:
    async def test_every_fact_of_one_close_stays_live(
        self, fact_store: FactStore,
    ):
        """The issue's reproduction: three tuples in, three live rows."""
        stored = await store_extracted_facts(
            fact_store,
            facts=[
                # The extractor canonicalises the subject, so a
                # capitalised variant lands under the same key.  Each
                # tuple also carries its own certainty, the only other
                # value that differs between the facts one close writes
                # about one topic; it must not decide which stay live.
                {
                    "subject": "Event Planning",
                    "predicate": "topic.decided",
                    "object": _BRIEFING[0],
                    "certainty": 1.0,
                },
                {
                    "subject": _TOPIC,
                    "predicate": "topic.decided",
                    "object": _BRIEFING[1],
                    "certainty": 0.95,
                },
                {
                    "subject": _TOPIC,
                    "predicate": "topic.decided",
                    "object": _BRIEFING[2],
                    "certainty": 0.9,
                },
            ],
            source_interaction_id="ix-1",
            # A real close time has a fractional second.
            asserted_at=2106900777.7129362,
            session_id="legacy",
        )
        assert stored == 3
        rows = await fact_store.recall(
            subject=_TOPIC, include_superseded=True,
        )
        assert len(rows) == 3
        assert [r.superseded_by for r in rows] == [None, None, None]
        assert {r.object for r in rows} == set(_BRIEFING)

    async def test_one_close_stamps_one_source_and_one_instant(
        self, fact_store: FactStore,
    ):
        """Pin (passes before the fix): the premise the rule stands on.

        "Written together" is read off the row as one source interaction
        plus one ``asserted_at``.  If the extractor ever stamped the rows
        of one close differently, the rule would stop recognising them.
        """
        await _extract(fact_store, _BRIEFING)
        rows = await fact_store.recall(
            subject=_TOPIC, include_superseded=True,
        )
        assert len(rows) == 3
        assert {r.source_interaction_id for r in rows} == {"ix-1"}
        assert {r.asserted_at for r in rows} == {1000.0}


class TestPromptListsFactsWrittenTogether:
    async def test_facts_section_lists_each_row_once_under_one_header(
        self, fact_store: FactStore,
    ):
        """All three constraints reach the prompt, under one header.

        The facts come through the recall step the runtime uses,
        which finds the topic from the words of the question.
        """
        await _extract(fact_store, _BRIEFING)
        question = "Where are we on event planning?"
        facts = await recall_facts_for_event(
            fact_store,
            AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": question},
                channel_id="group:standup",
                sender_id="bob",
            ),
            stimulus=question,
        )
        budget = MemoryBudget(total_tokens=500)
        section = render_facts_section(
            facts, budget, facts_budget_tokens=500,
        )
        assert section is not None
        assert section.content.count(f"Known facts about {_TOPIC}:") == 1
        assert [section.content.count(obj) for obj in _BRIEFING] == [1, 1, 1]
        # Recall lists the last-written first, and the section keeps
        # that order.
        positions = [section.content.index(obj) for obj in _BRIEFING]
        assert positions == sorted(positions, reverse=True)
        assert budget.admissions_by_tier("facts") == [
            f.fact_id for f in facts
        ]
        assert len(facts) == 3

    async def test_a_repeated_tuple_is_printed_once(
        self, fact_store: FactStore,
    ):
        """An extraction that repeats a tuple does not print it twice."""
        cap, crew = _BRIEFING[1], _BRIEFING[2]
        await _extract(fact_store, [cap, crew, cap])
        facts = await fact_store.recall(subject=_TOPIC)
        section = render_facts_section(
            facts, MemoryBudget(total_tokens=500), facts_budget_tokens=500,
        )
        assert section is not None
        assert section.content.count(cap) == 1
        assert section.content.count(crew) == 1


class TestAuditTrailForFactsWrittenTogether:
    async def test_one_extraction_records_no_supersession_and_a_later_write_records_each(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """The audit trail names a retraction only when there is one.

        Storing three facts together retracts nothing.  A later
        conversation that speaks about the same topic then retracts all
        three, and each retraction names the row that replaced it.
        """
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        await _extract(fact_store, _BRIEFING)
        earlier = {
            r.fact_id for r in await fact_store.recall(subject=_TOPIC)
        }
        assert len(earlier) == 3
        assert _supersede_events(caplog) == []

        await _extract(
            fact_store, ["venue moved to the town hall"],
            source="ix-2", at=2000.0,
        )
        (later,) = await fact_store.recall(subject=_TOPIC)
        events = _supersede_events(caplog)
        assert len(events) == 3
        # A set: the order in which one write retires several rows is
        # not specified.
        assert {e["superseded_fact_id"] for e in events} == earlier
        assert {e["by_fact_id"] for e in events} == {later.fact_id}
