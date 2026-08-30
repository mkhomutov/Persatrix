"""ISSUE-0131 — the ``speaker_id`` projection: derived memory records WHO.

Migration 18 landed the column dormant; this is its writer.  Derived
memory recorded WHAT was said and WHERE, never WHO — so a persona's own
restatement of a group discussion could attribute one speaker's
disclosure to another, which the v0.3.15 design gate reproduced live on
2026-08-21 (Phase 0b: one persona extracted an attribute of *iron-fox*
from a turn **ember-owl** spoke).

The fix is not to make attribution smarter — the Phase 0b scope lock
forbids model-elected attribution.  It is that the
``(principal, speaker, scope)`` record key makes every close-derived
record single-speaker BY CONSTRUCTION, so the column is a projection of
a key half rather than a judgement: ``interaction.speaker_id`` onto
``episodes.speaker_id`` and onto every fact the record's close extracts.

The premise has exactly one breach, and these pins are mostly about it.
The RFC 0020 §G room-close fan lands ONE closing message as the final
turn of EVERY record it closes, so on all but one of them that turn's
sender is not the record's speaker.  The RFC obliges this PR to
"exclude or tag" it; it is discharged as EXCLUDE, upstream of the
combined summarise+extract call, so no fact can be derived from another
speaker's words and then stamped with this record's.

The fact-tier half of the projection is pinned in
:mod:`.test_speaker_projection_facts` (split at the 500-line cap); the
record builders below are shared with it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from agents.llm_client import LLMResponse, StopReason, Usage
from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.interaction_tracker import InteractionTracker
from agents.memory.interaction_types import ROOM_CLOSE_TURN_KEY, Interaction, Turn
from agents.persona_runtime import close_path
from agents.persona_runtime.close_entries import interaction_to_entries
from agents.persona_runtime.episode_routing import _EpisodeRoutingMixin
from agents.persona_runtime.summarize_close import (
    summarize_closed_interaction,
)
from agents.persona_runtime.turn_payload import build_turn_payload
from agents.persona_types import AgentEvent, EventType

_SCOPE = "group:planning"


def _record(speaker: str, turns: list[Turn] | None = None) -> Interaction:
    return Interaction(
        interaction_id=f"i-{speaker or 'none'}",
        scope=_SCOPE,
        started_at=1_000.0,
        closed_at=1_100.0,
        close_reason=REASON_STRUCTURAL,
        speaker_id=speaker,
        turns=turns if turns is not None else [
            Turn(at=1_000.0, payload={"sender": speaker, "text": "my own words"}),
        ],
    )


def _room_close_turn(sender: str) -> Turn:
    """The turn the close-notification fan lands on every record."""
    return Turn(at=1_090.0, payload={
        "sender": sender,
        "text": "we are dropping Postgres for the ledger",
        ROOM_CLOSE_TURN_KEY: True,
    })


class TestTheSGExclusion:
    """The premise-preserving half: a foreign room-close turn never
    reaches the derivation input."""

    def test_a_foreign_room_close_turn_is_dropped(self):
        record = _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "amber-lynx", "text": "mine"}),
            _room_close_turn("iron-fox"),
        ])

        contents = [e.content for e in interaction_to_entries(record)]

        assert len(contents) == 1
        assert "mine" in contents[0]
        assert not any("Postgres" in c for c in contents), (
            "a fact from the closer's words would be stamped with THIS "
            "record's speaker — the misattribution the key exists to prevent"
        )

    def test_the_closers_own_record_keeps_it(self):
        """The turn is foreign only where the sender is not the record's
        speaker.  On the closer's own record it is native — iron-fox
        really did say it into the conversation that record holds — so
        the content survives wherever it is attributable."""
        record = _record("iron-fox", [
            Turn(at=1_000.0, payload={"sender": "iron-fox", "text": "mine"}),
            _room_close_turn("iron-fox"),
        ])

        contents = [e.content for e in interaction_to_entries(record)]

        assert len(contents) == 2
        assert any("Postgres" in c for c in contents)

    def test_an_ordinary_turn_from_another_sender_is_kept(self):
        """Only the STAMPED turn is excluded.  A record whose turns carry
        a different sender for any other reason is not silently emptied —
        the stamp is the discriminator, not the sender comparison alone."""
        record = _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "someone-else", "text": "kept"}),
        ])

        assert len(interaction_to_entries(record)) == 1

    def test_ordinals_do_not_renumber_around_a_dropped_turn(self):
        """Importance is the turn's real position, so excluding one does
        not re-weight its siblings."""
        record = _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "amber-lynx", "text": "first"}),
            _room_close_turn("iron-fox"),
            Turn(at=1_099.0, payload={"sender": "amber-lynx", "text": "third"}),
        ])

        entries = interaction_to_entries(record)

        assert [e.id for e in entries] == ["turn-1", "turn-3"]

    def test_the_fans_real_payload_is_what_gets_excluded(self):
        """The predicate is a conjunction over two fields the
        close-notification fan writes (``build_turn_payload(...,
        room_close=True)``).  The other pins hand-build that payload, so
        only this one fails if the producer stops writing ``sender``
        verbatim or moves the stamp — the seam where the fan and the
        exclusion could drift apart with every other test green."""
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "we are dropping Postgres for the ledger"},
            channel_id="group:planning",
            sender_id="iron-fox",
        )
        fanned = build_turn_payload(
            event, "Event: channel_message → Actions: []", room_close=True,
        )
        record = _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "amber-lynx", "text": "mine"}),
            Turn(at=1_090.0, payload=fanned),
        ])

        contents = [e.content for e in interaction_to_entries(record)]

        assert len(contents) == 1
        assert not any("Postgres" in c for c in contents)


class TestEpisodeProjection:
    async def test_the_row_carries_the_records_speaker(self, memory, monkeypatch):
        await _persist(memory, _record("amber-lynx"), monkeypatch)

        assert await _episode_speaker(memory, "i-amber-lynx") == "amber-lynx"

    async def test_a_speakerless_scope_is_null_not_empty(self, memory, monkeypatch):
        """``""`` is the no-speaker convention (tick / single-turn scope).
        NULL is the honest column value; an empty string would read as an
        attribution to a speaker named nothing."""
        await _persist(memory, _record(""), monkeypatch)

        assert await _episode_speaker(memory, "i-none") is None


class TestPersistedContextAppliesTheExclusion:
    """The §G drop covers persistence too, not just the derivation
    input (PR #849 review): ``context_json`` is an FTS-indexed column
    recall searches, so a foreign room-close turn's sender and envelope
    must not ride it on a row stamped with another speaker."""

    async def test_a_foreign_close_turn_is_not_persisted(
        self, memory, monkeypatch,
    ):
        await _persist(memory, _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "amber-lynx"}),
            _room_close_turn("iron-fox"),
        ]), monkeypatch)

        assert await _persisted_turn_senders(
            memory, "i-amber-lynx",
        ) == ["amber-lynx"]

    async def test_the_closers_own_record_persists_it(
        self, memory, monkeypatch,
    ):
        """Same rule as the derivation drop: the turn is native on the
        closer's own record, so its persisted context keeps it."""
        await _persist(memory, _record("iron-fox", [
            Turn(at=1_000.0, payload={"sender": "iron-fox"}),
            _room_close_turn("iron-fox"),
        ]), monkeypatch)

        assert await _persisted_turn_senders(
            memory, "i-iron-fox",
        ) == ["iron-fox", "iron-fox"]

    async def test_the_persisted_turn_count_counts_what_the_row_holds(
        self, memory, monkeypatch,
    ):
        """PR #849 review round 3: the ``turn_count`` column (and the
        context copy) count the post-§G-exclusion turns, the same rule
        as the prompt header's ``shown_turns`` — the record's raw count
        would admit a single-native-turn sibling past ``min_turns``
        filters as multi-turn on the strength of a turn the row does
        not hold."""
        await _persist(memory, _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "amber-lynx"}),
            _room_close_turn("iron-fox"),
        ]), monkeypatch)

        db = memory._ensure_db()
        async with db.execute(
            "SELECT turn_count, context_json FROM episodes "
            "WHERE interaction_id = ?", ("i-amber-lynx",),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None, "the close path wrote no episode"
        assert row[0] == 1
        assert json.loads(row[1])["turn_count"] == 1


class TestTheStorageBoundaryNormalizesTheSpeaker:
    async def test_an_empty_string_persists_as_null(self, memory):
        """``"" == no speaker → NULL`` is enforced in ``insert_episode``
        itself, so a direct caller bypassing the projection sites'
        ``or None`` discipline cannot mint a third speaker state."""
        await memory.store_episode(
            summary="direct write", context={}, speaker_id="",
        )

        db = memory._ensure_db()
        async with db.execute(
            "SELECT speaker_id FROM episodes WHERE summary = ?",
            ("direct write",),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None


async def _noop() -> None:
    return None


async def _skip_finalize(**kwargs: object) -> None:
    return None


async def _persist(memory, interaction: Interaction, monkeypatch) -> None:
    """Phase-1-only close — the sibling ``_persist`` / ``_no_phase_two``
    idiom (``test_close_path_principal_binding``), fused because every
    caller here stubs Phase 2.  One copy of the call shape, so a new
    ``persist_closed_interaction`` parameter is threaded once."""
    monkeypatch.setattr(
        close_path, "finalize_closed_interaction", _skip_finalize,
    )
    await close_path.persist_closed_interaction(
        episodic=memory, llm_client=MagicMock(), memory_ns=MagicMock(),
        agent_id="test-agent", interaction=interaction,
        pending_tasks=set(), on_finalized=_noop,
    )


async def _persisted_turn_senders(memory, interaction_id: str) -> list[str]:
    db = memory._ensure_db()
    async with db.execute(
        "SELECT context_json FROM episodes WHERE interaction_id = ?",
        (interaction_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the close path wrote no episode"
    return [t["payload"].get("sender") for t in json.loads(row[0])["turns"]]


async def _episode_speaker(memory, interaction_id: str) -> str | None:
    db = memory._ensure_db()
    async with db.execute(
        "SELECT speaker_id FROM episodes WHERE interaction_id = ?",
        (interaction_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the close path wrote no episode"
    return row[0]


class TestAllExcludedDegradesCheaply:
    async def test_a_record_of_only_foreign_turns_takes_the_placeholder(self):
        """Unreachable today — a record opens by taking a turn of its own,
        and the fan only appends to records already open — so this guards
        the exclusion's blast radius: a future ingest that lands a fan
        turn on an empty record must not send an empty prompt to the LLM."""
        record = _record("amber-lynx", [_room_close_turn("iron-fox")])
        llm = MagicMock()

        summary, failed, facts_raw, projections = (
            await summarize_closed_interaction(
                llm_client=llm, interaction=record, agent_id="test-agent",
            )
        )

        assert failed is False
        assert facts_raw is None and projections == {}
        assert "no content attributable" in summary
        llm.complete.assert_not_called()

    async def test_a_lone_foreign_turn_skips_the_single_turn_fast_path(self):
        """The ``turn_count == 1`` fast path reads ``turns[0]`` before the
        exclusion runs, so it needs the same §G guard.

        The turn that reaches it is the dangerous shape: an action
        envelope (``summary``) with no message body (``text``), which is
        the only combination that fast path accepts.  Without the guard
        this record's episode would be summarised from **iron-fox**'s
        closing turn and then stamped ``speaker_id='amber-lynx'`` by
        ``close_path`` — the Phase 0b misattribution, reintroduced by the
        one path the exclusion did not cover."""
        record = _record("amber-lynx", [Turn(at=1_090.0, payload={
            "sender": "iron-fox",
            "summary": "Event: chat_end → Actions: []",
            ROOM_CLOSE_TURN_KEY: True,
        })])
        llm = MagicMock()

        summary, failed, facts_raw, _ = await summarize_closed_interaction(
            llm_client=llm, interaction=record, agent_id="test-agent",
        )

        assert "no content attributable" in summary
        assert "iron-fox" not in summary and "chat_end" not in summary
        assert failed is False and facts_raw is None
        llm.complete.assert_not_called()


class TestThePromptCountsWhatItShows:
    """PR #849 review: ``Turns:`` reported ``interaction.turn_count`` —
    2 here — while the §G exclusion left one entry in the ``Compressed
    turns`` block, inviting the model to narrate a turn it is never
    shown, in the one call whose output ``close_path`` then stamps with
    this record's ``speaker_id``.  The header must count what the
    prompt actually holds."""

    async def test_turns_header_matches_the_derivation_input(self):
        record = _record("amber-lynx", [
            Turn(at=1_000.0, payload={"sender": "amber-lynx", "text": "mine"}),
            _room_close_turn("iron-fox"),
        ])
        llm = MagicMock()
        llm.create_message = AsyncMock(return_value=LLMResponse(
            text=json.dumps({"summary": "s", "facts": []}),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        ))

        await summarize_closed_interaction(llm, "test-agent", record)

        prompt = llm.create_message.call_args.kwargs["messages"][0]["content"]
        assert "Turns: 1\n" in prompt
        assert "iron-fox" not in prompt


class _SingleTurnHarness(_EpisodeRoutingMixin):
    """The mixin's single-turn routing over a real tracker + real store.

    Only the attributes ``_store_event_episode`` touches on the
    single-turn path; the idle sweep runs first and is empty on a fresh
    tracker, so ``_persist_closed_interaction`` is never reached.
    """

    def __init__(self, episodic) -> None:
        self.agent_id = "test-agent"
        self._episodic_memory = episodic
        self._interaction_tracker = InteractionTracker()
        self._session_id = "legacy"


class TestSingleTurnProjection:
    """The third write site.  A single-turn scope opens and closes inside
    one call, so it never reaches ``persist_closed_interaction`` — it
    stamps its own row and needs its own pin."""

    async def test_a_single_turn_event_stamps_its_sender(self, memory):
        harness = _SingleTurnHarness(memory)

        await harness._store_event_episode(
            AgentEvent(
                event_type=EventType.TASK_ASSIGNED, sender_id="amber-lynx",
            ),
            [],
        )

        assert await _episode_speaker_by_scope(
            memory, EventType.TASK_ASSIGNED.value,
        ) == "amber-lynx"

    async def test_a_tick_has_no_speaker_and_stamps_null(self, memory):
        """A tick genuinely has no sender — NULL, not an empty string."""
        harness = _SingleTurnHarness(memory)

        await harness._store_event_episode(
            AgentEvent(event_type=EventType.TICK), [],
        )

        assert await _episode_speaker_by_scope(
            memory, EventType.TICK.value,
        ) is None


async def _episode_speaker_by_scope(memory, scope: str) -> str | None:
    db = memory._ensure_db()
    async with db.execute(
        "SELECT speaker_id FROM episodes WHERE scope = ?", (scope,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, f"no episode written for scope {scope!r}"
    return row[0]
