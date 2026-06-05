"""Person-keyed (contact) notes recall cross-room (v0.3.7 conversation
test-findings PR plan, F-3b / PR 5; amends RFC 0031 §D).

`docs/memory-scope-axes.md` separates two axes: a **session = room**
(notes scoped `(agent, channel)`, isolated by default) and the
**relationship / person** axis, which is cross-room by design — "who is
this person, to me?" attaches to the person, not the venue. The
`memory-tool-usage` snippet has the persona save a person's identity as a
note under topic ``contact:<participant_id>``, but those notes inherited
the room-scoped recall default, so a persona that learned your name in one
channel could not recall it in another (live repro: "I don't have any
notes about your name" in a fresh channel).

This adds ``recall_contact_notes(participant_id)`` — an exact-topic,
**cross-session** recall that is still scoped to the active
``principal_id`` / ``epoch_id`` (cross-*room*, never cross-tenant or
cross-epoch). General/room notes keep the §D default
(``session_id IN (active, legacy)``), so room isolation is untouched.

These tests pin the four properties that make the carve-out safe and
narrow: cross-room visibility, topic-exactness, preserved room isolation
for non-contact notes, and epoch scoping.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.clock import WallClock
from agents.epoch_id import EPOCH_ID_ENV_VAR
from agents.memory.episodic import EpisodicMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType
from agents.session_id import SESSION_ID_ENV_VAR


async def _store(path: str, *, session: str, topic: str, content: str,
                 epoch: str | None, monkeypatch) -> None:
    monkeypatch.setenv(SESSION_ID_ENV_VAR, session)
    if epoch is not None:
        monkeypatch.setenv(EPOCH_ID_ENV_VAR, epoch)
    mem = EpisodicMemory(agent_id="shared-agent", db_path=path)
    await mem.initialize()
    try:
        await mem.store_note(topic, content, session_id=session)
    finally:
        await mem.close()


async def _open_at(path: str, *, session: str, epoch: str | None,
                   monkeypatch) -> EpisodicMemory:
    monkeypatch.setenv(SESSION_ID_ENV_VAR, session)
    if epoch is not None:
        monkeypatch.setenv(EPOCH_ID_ENV_VAR, epoch)
    mem = EpisodicMemory(agent_id="shared-agent", db_path=path)
    await mem.initialize()
    return mem


class TestContactNoteCrossRoomRecall:
    async def test_contact_note_visible_across_sessions(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``contact:<id>`` note saved in room A is recalled in room B."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="contact:alice",
                content="Name: Alice. Favorite language: Rust.",
                epoch=None, monkeypatch=monkeypatch,
            )
            mem_b = await _open_at(
                path, session="room-b", epoch=None, monkeypatch=monkeypatch,
            )
            try:
                notes = await mem_b.recall_contact_notes("alice")
            finally:
                await mem_b.close()
            assert [n.content for n in notes] == [
                "Name: Alice. Favorite language: Rust.",
            ]

    async def test_contact_recall_is_topic_exact(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recalling alice must not leak bob's contact note."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="contact:bob",
                content="Name: Bob.", epoch=None, monkeypatch=monkeypatch,
            )
            mem_b = await _open_at(
                path, session="room-b", epoch=None, monkeypatch=monkeypatch,
            )
            try:
                assert await mem_b.recall_contact_notes("alice") == []
            finally:
                await mem_b.close()

    async def test_room_note_stays_session_scoped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-contact (room) note saved in room A is NOT recalled in
        room B via the default query path — the cross-room carve-out is
        contact-only; general room isolation is preserved.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="sprint-plan",
                content="ship the widget by Friday",
                epoch=None, monkeypatch=monkeypatch,
            )
            mem_b = await _open_at(
                path, session="room-b", epoch=None, monkeypatch=monkeypatch,
            )
            try:
                hits = await mem_b.recall_notes("widget", sessions=None)
            finally:
                await mem_b.close()
            assert hits == []

    async def test_contact_recall_respects_epoch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cross-room, never cross-epoch: a contact note saved under epoch
        e1 is invisible to a recall running under epoch e2.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="contact:alice",
                content="Name: Alice.", epoch="e1", monkeypatch=monkeypatch,
            )
            mem_b = await _open_at(
                path, session="room-b", epoch="e2", monkeypatch=monkeypatch,
            )
            try:
                assert await mem_b.recall_contact_notes("alice") == []
            finally:
                await mem_b.close()


class _ContactMixin(_MemoryContextMixin):
    def __init__(self) -> None:
        super().__init__()
        self._clock = WallClock()
        self._timezone = "UTC"

    def _format_event(self, event):  # type: ignore[override]
        # A query that does NOT lexically match the contact note, to prove
        # the contact recall is sender/topic-driven, not query-driven.
        return "rollout planning status"


class TestContactNoteInjectedCrossRoom:
    """``_inject_memory_context`` recalls the event sender's contact note
    cross-room and injects it even when the inbound query does not match
    the note text — the F-3 repro ("how do you know my name?" in a new
    room) becomes "knows your name regardless of the room".
    """

    async def _wire(self, episodic: EpisodicMemory, sender_id: str):
        mixin = _ContactMixin()
        mixin.agent_id = "shared-agent"
        mixin._working_memory = WorkingMemory(max_tokens=8192)
        mixin._episodic_memory = episodic
        mixin._relationship_memory = AsyncMock()
        mixin._relationship_memory.get_relationship_summary.return_value = None
        mixin._fact_store = None
        event = MagicMock()
        event.event_type = EventType.CHANNEL_MESSAGE
        event.channel_id = "group:other-room"
        event.sender_id = sender_id
        event.thread_id = None
        event.metadata = {"sender_participant_type": "user"}
        event.payload = {"content": "rollout planning status",
                         "channel_type": "group"}
        event.timestamp = 0.0
        return mixin, event

    async def test_sender_contact_note_injected_despite_query_miss(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "room-a")
        mem = EpisodicMemory(agent_id="shared-agent", db_path=":memory:")
        await mem.initialize()
        try:
            await mem.store_note(
                "contact:alice-uid", "Name: Alice. Favorite language: Rust.",
                session_id="room-a",
            )
            # Recall runs as if we are now in a different room.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "room-b")
            mixin, event = await self._wire(mem, "alice-uid")
            await mixin._inject_memory_context(event)
            section = mixin._working_memory.get_section("recent_notes")
            assert section is not None, "contact note was not injected"
            assert "Name: Alice" in section.content
        finally:
            await mem.close()
