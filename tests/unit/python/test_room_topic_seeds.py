"""A room's own subject seeds fact recall (ISSUE-0180).

A topic fact used to reach a later prompt only when the incoming message
repeated its stored subject word for word, so a briefing about a client
was not recalled on a later turn that said "please critique this plan".
Stored subjects that the room's description names now seed recall on
every turn in that room.  Three surfaces:

* :func:`agents.persona_runtime.topic_seeds.topic_recall_seeds` — which
  subjects seed, in what order, and how each is to be read;
* :func:`agents.persona_runtime.facts_section.recall_facts_for_event`
  with ``room_text=`` — the wired read, including that a long list under
  the room's subject keeps the facts it was first told;
* the shadow pass, which must count the same seeds.

The amendment's bounds still hold and are pinned again here for the new
seeds: topic predicates only, three seeds in all, and the eligibility
rule.
"""

from __future__ import annotations

import logging

import pytest

from agents.memory.fact_predicates import TOPIC_PREDICATES
from agents.memory.facts import FactStore
from agents.persona_runtime.facts_section import (
    DEFAULT_FACTS_BUDGET_TOKENS,
    recall_facts_for_event,
    render_facts_section,
)
from agents.persona_runtime.facts_shadow import (
    CROSS_ROOM_SHADOW,
    SHADOW_LOGGER_NAME,
    SHADOW_TRACE_ATTR,
    emit_facts_shadow,
)
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_runtime.topic_seeds import (
    ROOM_SEED_LIMIT,
    TOPIC_SEED_LIMIT,
    topic_recall_seeds,
    topic_subject_seeds,
)
from agents.persona_types import AgentEvent, EventType

_asyncio = pytest.mark.asyncio

ROOM = "Riverside Clinic, a walk-in practice with twelve staff"
CLINIC = "riverside clinic"
PLAN_MESSAGE = "Please critique this plan and recommend one option."
BRIEFING = (
    "car park closes at six every evening",
    "no appointment may cost more than $40",
    "reception is down to two people",
)


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _topic(
    store: FactStore, subject: str, obj: str, *, at: float,
    room: str = "room-1", predicate: str = "topic.has_status",
    source: str | None = None,
) -> str:
    """One topic fact, taught in ``room`` (a room is its own session)."""
    return await store.store(
        subject=subject, predicate=predicate, object=obj,
        source_interaction_id=source or f"{room}-{at}", asserted_at=at,
        session_id=room,
    )


async def _briefing(store: FactStore) -> None:
    """Three facts from one extraction: one source, one instant."""
    for obj in BRIEFING:
        await _topic(store, CLINIC, obj, at=1000.0, room="briefing", source="briefing")


async def _later_rooms(store: FactStore, rooms: int, facts_each: int) -> None:
    """Later rooms that each file their own facts under the same subject."""
    for r in range(1, rooms + 1):
        for n in range(facts_each):
            await _topic(
                store, CLINIC, f"plan {r} point {n}: agreed at the weekly review",
                at=2000.0 + r, room=f"plan-{r}", source=f"plan-{r}",
            )


def _event(content: str = PLAN_MESSAGE, sender: str = "operator") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content},
        channel_id="group:plan-9",
        sender_id=sender,
        metadata={"channel_classification": "internal"},
    )


async def _seeds(store, stimulus, **kwargs):
    kwargs.setdefault("exclude", set())
    kwargs.setdefault("sessions", "*")
    return await topic_recall_seeds(store, stimulus, **kwargs)


# ─── topic_recall_seeds ─────────────────────────────────────


@_asyncio
class TestRoomSeeds:
    async def test_the_rooms_subject_seeds_without_a_mention(self, fact_store):
        """The ISSUE-0180 case: nothing in the message names the subject."""
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        assert await _seeds(fact_store, PLAN_MESSAGE, room_text=ROOM) == [
            (CLINIC, TOPIC_PREDICATES, "both_ends"),
        ]

    async def test_without_room_text_only_the_message_seeds(self, fact_store):
        """No description, no change: the shipped rule, and read newest first."""
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        assert await _seeds(fact_store, PLAN_MESSAGE) == []
        assert await _seeds(fact_store, "any news on Riverside Clinic?") == [
            (CLINIC, TOPIC_PREDICATES, "newest"),
        ]

    async def test_room_seeds_come_before_the_messages(self, fact_store):
        """The store lists ``atlas`` first, as the more recent subject.  The
        room's own subject still takes the first slot, so the facts section,
        which spends its budget in seed order, reaches it first."""
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        await _topic(fact_store, "atlas", "ships friday", at=2000.0)
        assert await _seeds(
            fact_store, "where are we on atlas?", room_text=ROOM,
        ) == [
            (CLINIC, TOPIC_PREDICATES, "both_ends"),
            ("atlas", TOPIC_PREDICATES, "newest"),
        ]

    async def test_a_subject_both_texts_name_seeds_once_as_the_rooms(self, fact_store):
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        assert await _seeds(
            fact_store, "what does Riverside Clinic need?", room_text=ROOM,
        ) == [(CLINIC, TOPIC_PREDICATES, "both_ends")]

    async def test_three_seeds_in_all_and_one_slot_stays_the_messages(self, fact_store):
        """A description that names many stored subjects cannot silence the
        message: the room takes at most ``ROOM_SEED_LIMIT`` slots, its most
        recently asserted subjects first, and the cap on the whole is the
        amendment's three."""
        room = "Riverside Clinic: reception, pharmacy and car park"
        for at, subject in enumerate(
                ["reception", "pharmacy", "car park", "atlas", "borealis"]):
            await _topic(fact_store, subject, "open", at=1000.0 + at)
        seeds = await _seeds(
            fact_store, "atlas and borealis both slipped", room_text=room,
        )
        assert [(s, order) for s, _p, order in seeds] == [
            ("car park", "both_ends"), ("pharmacy", "both_ends"),
            ("borealis", "newest"),
        ]
        assert ROOM_SEED_LIMIT == 2
        assert len(seeds) == TOPIC_SEED_LIMIT == 3

    async def test_the_message_fills_slots_the_room_leaves(self, fact_store):
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        for at, subject in enumerate(["atlas", "borealis", "cygnus"]):
            await _topic(fact_store, subject, "open", at=2000.0 + at)
        seeds = await _seeds(
            fact_store, "atlas, borealis and cygnus", room_text=ROOM,
        )
        assert [s for s, _p, _o in seeds] == [CLINIC, "cygnus", "borealis"]

    async def test_a_turn_with_no_message_text_still_seeds_the_room(self, fact_store):
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        for stimulus in (None, "", "   ", 123):
            assert await _seeds(fact_store, stimulus, room_text=ROOM) == [
                (CLINIC, TOPIC_PREDICATES, "both_ends"),
            ]

    async def test_with_neither_text_the_store_is_not_read(self, fact_store):
        calls: list[str] = []

        async def _spy(**kwargs):
            calls.append("topic_subjects")
            return []

        fact_store.topic_subjects = _spy  # type: ignore[method-assign]
        for room_text in (None, "", "   ", 123):
            assert await _seeds(fact_store, "", room_text=room_text) == []
        assert calls == []

    async def test_a_person_seed_is_never_also_a_room_seed(self, fact_store):
        """``exclude`` holds the person seeds, which read every predicate.
        A description that names the sender must not add a second, narrower
        read of the same subject."""
        await _topic(fact_store, "bob", "atlas", at=1000.0, predicate="topic.owned_by")
        assert await _seeds(
            fact_store, "hello", room_text="Bob and Ann's planning room",
            exclude={"bob"},
        ) == []

    async def test_room_matching_is_whole_subject_whole_word_and_folded(self, fact_store):
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        for room in ("RIVERSIDE   CLINIC (north site)", "the riverside clinic's annexe"):
            assert [s for s, _p, _o in await _seeds(fact_store, "", room_text=room)] == [CLINIC]
        for room in ("Riverside Clinics group", "Riverside walk-in clinic", "Riverside"):
            assert await _seeds(fact_store, "", room_text=room) == []

    async def test_a_subject_that_may_not_seed_does_not_seed_from_the_room(self, fact_store):
        """The eligibility rule is the same for both texts: a planted
        subject of a function word, or one too short, never seeds."""
        await _topic(fact_store, "the", "planted", at=1000.0)
        await _topic(fact_store, "qa", "planted", at=1001.0)
        assert await _seeds(fact_store, "", room_text="the qa room") == []

    async def test_enumeration_failure_seeds_nothing(self):
        class _Boom:
            agent_id = "test-agent"

            async def topic_subjects(self, **kwargs):
                raise RuntimeError("db gone")

        assert await _seeds(_Boom(), PLAN_MESSAGE, room_text=ROOM) == []
        assert await _seeds(None, PLAN_MESSAGE, room_text=ROOM) == []

    async def test_the_subject_list_view_matches(self, fact_store):
        """``topic_subject_seeds`` is the same seeds without how to read them."""
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        await _topic(fact_store, "atlas", "ships friday", at=2000.0)
        assert await topic_subject_seeds(
            fact_store, "where are we on atlas?", exclude=set(),
            sessions="*", room_text=ROOM,
        ) == [CLINIC, "atlas"]
        assert await topic_subject_seeds(
            fact_store, "where are we on atlas?", exclude=set(), sessions="*",
        ) == ["atlas"]


# ─── recall_facts_for_event(room_text=) ─────────────────────


def _render(facts) -> str:
    section = render_facts_section(
        facts, MemoryBudget(total_tokens=1500),
        facts_budget_tokens=DEFAULT_FACTS_BUDGET_TOKENS,
    )
    return section.content if section is not None else ""


@_asyncio
class TestRecallWithRoomText:
    async def test_a_briefing_reaches_a_turn_that_does_not_name_it(self, fact_store):
        await _briefing(fact_store)
        without = await recall_facts_for_event(
            fact_store, _event(), stimulus=PLAN_MESSAGE, sessions="*",
        )
        assert without == []
        facts = await recall_facts_for_event(
            fact_store, _event(), stimulus=PLAN_MESSAGE, sessions="*", room_text=ROOM,
        )
        assert sorted(f.object for f in facts) == sorted(BRIEFING)

    async def test_a_room_seed_reads_only_topic_rows(self, fact_store):
        """The amendment's bound, for the new seed: a person predicate filed
        under the room's subject stays out of a room-seeded read."""
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        await _topic(fact_store, CLINIC, "late invoices", at=1000.0, predicate="dislikes")
        facts = await recall_facts_for_event(
            fact_store, _event(), stimulus=PLAN_MESSAGE, sessions="*", room_text=ROOM,
        )
        assert [f.predicate for f in facts] == ["topic.has_status"]

    async def test_order_is_person_then_room_then_message(self, fact_store):
        await fact_store.store(
            subject="self", predicate="self.holds_value", object="candour",
            source_interaction_id="int-1", asserted_at=3000.0,
        )
        await _topic(fact_store, CLINIC, BRIEFING[0], at=1000.0)
        await _topic(fact_store, "atlas", "ships friday", at=2000.0)
        facts = await recall_facts_for_event(
            fact_store, _event("where are we on atlas?"),
            stimulus="where are we on atlas?", sessions="*", room_text=ROOM,
        )
        assert [f.subject for f in facts] == ["self", CLINIC, "atlas"]

    async def test_a_senderless_event_still_reads_nothing(self, fact_store):
        """The empty-context cost guard: a tick in a described room issues
        no fact read at all."""
        await _briefing(fact_store)
        calls: list[str] = []
        original = fact_store.topic_subjects

        async def _spy(**kwargs):
            calls.append("topic_subjects")
            return await original(**kwargs)

        fact_store.topic_subjects = _spy  # type: ignore[method-assign]
        tick = AgentEvent(event_type=EventType.TICK, payload={})
        assert await recall_facts_for_event(
            fact_store, tick, stimulus="riverside clinic", room_text=ROOM,
        ) == []
        assert calls == []

    async def test_the_first_told_facts_survive_the_row_cap(self, fact_store):
        """Twenty-five later facts under the same subject.  The read still
        returns twenty rows, and the briefing's three are among them."""
        await _briefing(fact_store)
        await _later_rooms(fact_store, rooms=5, facts_each=5)
        facts = await recall_facts_for_event(
            fact_store, _event(), stimulus=PLAN_MESSAGE, sessions="*", room_text=ROOM,
        )
        assert len(facts) == 20
        assert set(BRIEFING) <= {f.object for f in facts}

    async def test_the_first_told_facts_survive_the_token_budget(self, fact_store):
        """The facts section stops at its 200 tokens.  Read from both ends,
        the room's subject keeps the briefing; the same rows read newest
        first, as a message seed reads them, lose all three."""
        await _briefing(fact_store)
        await _later_rooms(fact_store, rooms=4, facts_each=5)
        room_seeded = _render(await recall_facts_for_event(
            fact_store, _event(), stimulus=PLAN_MESSAGE, sessions="*", room_text=ROOM,
        ))
        for obj in BRIEFING:
            assert obj in room_seeded
        assert "plan 4 point 4" in room_seeded      # the newest is kept too

        named = "what did Riverside Clinic agree?"
        message_seeded = _render(await recall_facts_for_event(
            fact_store, _event(named), stimulus=named, sessions="*",
        ))
        assert "plan 4 point 4" in message_seeded
        for obj in BRIEFING:
            assert obj not in message_seeded


# ─── the shadow pass counts the same seeds ──────────────────


@_asyncio
class TestShadowPassSeesRoomSeeds:
    async def test_a_room_seeded_fact_from_another_room_is_a_shadow_candidate(
        self, fact_store, caplog: pytest.LogCaptureFixture,
    ):
        """In ``shadow`` mode the live read stays inside the room and the
        shadow pass reports what the widened read would add.  It derives its
        own seeds, so it has to be given the room's text as well."""
        await _briefing(fact_store)
        with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER_NAME):
            await emit_facts_shadow(
                fact_store, _event(), stimulus=PLAN_MESSAGE, room_text=ROOM,
                live_fact_ids=set(), agent_id="test-agent", mode=CROSS_ROOM_SHADOW,
            )
        traces = [getattr(r, SHADOW_TRACE_ATTR) for r in caplog.records
                  if hasattr(r, SHADOW_TRACE_ATTR)]
        assert len(traces) == 1
        assert {c["subject"] for c in traces[0]["candidates"]} == {CLINIC}
        assert len(traces[0]["candidates"]) == len(BRIEFING)

    async def test_the_shadow_read_keeps_the_same_rows_under_the_cap(
        self, fact_store, caplog: pytest.LogCaptureFixture,
    ):
        """Past twenty rows the order decides which ones a read returns.
        The shadow pass reads the room's subject from both ends too, so its
        candidates are the rows the live read would have had."""
        await _briefing(fact_store)
        await _later_rooms(fact_store, rooms=5, facts_each=5)
        live = await recall_facts_for_event(
            fact_store, _event(), stimulus=PLAN_MESSAGE, sessions="*", room_text=ROOM,
        )
        with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER_NAME):
            await emit_facts_shadow(
                fact_store, _event(), stimulus=PLAN_MESSAGE, room_text=ROOM,
                live_fact_ids=set(), agent_id="test-agent", mode=CROSS_ROOM_SHADOW,
            )
        (trace,) = [getattr(r, SHADOW_TRACE_ATTR) for r in caplog.records
                    if hasattr(r, SHADOW_TRACE_ATTR)]
        assert [c["fact_id"] for c in trace["candidates"]] == [f.fact_id for f in live]
