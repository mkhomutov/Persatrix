"""Notes recall is uniformly room-scoped again (RFC 0031 amendment — F-7
Option D, ISSUE-0093 **PR D3**, retiring the Option-A carve-out).

F-7 PR A (#550) special-cased ``contact:*`` *note* recall to bypass the
session filter so a name learned in one room surfaced in another — a
topic-prefix workaround threaded through recall.  Option D re-homed person
identity onto the cross-room **relationship** tier (PR D1/D2), so that
workaround is no longer needed and is retired here: ``recall_notes`` is a
single room-scoped shape again, and the dedicated cross-room
``recall_contact_notes`` helper is gone.

These tests pin the *retirement* contract — the inverse of the F-3b tests
they replace:

* a ``contact:*`` note saved in room A is **not** recalled in room B (the
  carve-out is gone; notes are room-scoped like any other);
* the ``recall_contact_notes`` public method no longer exists;
* the per-event injection no longer surfaces a sender's contact note
  cross-room (cross-room identity now rides the relationship tier — see
  ``test_identity_render.TestIdentityImmediacyCrossRoom``).
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.clock import WallClock
from agents.memory.episodic import EpisodicMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType
from agents.session_id import SESSION_ID_ENV_VAR


async def _store(path: str, *, session: str, topic: str, content: str,
                 monkeypatch) -> None:
    monkeypatch.setenv(SESSION_ID_ENV_VAR, session)
    mem = EpisodicMemory(agent_id="shared-agent", db_path=path)
    await mem.initialize()
    try:
        await mem.store_note(topic, content, session_id=session)
    finally:
        await mem.close()


async def _open_at(path: str, *, session: str, monkeypatch) -> EpisodicMemory:
    monkeypatch.setenv(SESSION_ID_ENV_VAR, session)
    mem = EpisodicMemory(agent_id="shared-agent", db_path=path)
    await mem.initialize()
    return mem


class TestContactNoteRoomScoped:
    async def test_contact_note_not_recalled_across_rooms(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``contact:<id>`` note saved in room A is NOT recalled in room B:
        the F-7 Option-A cross-room note carve-out is retired, so contact
        notes are room-scoped like any other note."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="contact:alice",
                content="Name: Alice. Favorite language: Rust.",
                monkeypatch=monkeypatch,
            )
            mem_b = await _open_at(
                path, session="room-b", monkeypatch=monkeypatch,
            )
            try:
                # Both the lexical query path and the recency path are
                # room-scoped now — neither sees room-a's contact note.
                assert await mem_b.recall_notes("Alice", sessions=None) == []
                assert await mem_b.recall_notes("", sessions=None) == []
            finally:
                await mem_b.close()

    async def test_contact_note_recalled_in_same_room(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same-room recall is unchanged — a contact note is still a normal
        note in the room it was written in."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="contact:alice",
                content="Name: Alice.", monkeypatch=monkeypatch,
            )
            mem_a = await _open_at(
                path, session="room-a", monkeypatch=monkeypatch,
            )
            try:
                hits = await mem_a.recall_notes("Alice", sessions=None)
            finally:
                await mem_a.close()
            assert [n.content for n in hits] == ["Name: Alice."]

    async def test_non_contact_room_note_stays_room_scoped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A plain (non-contact) room note was always room-scoped and still
        is — the retirement narrows contact notes back to this same rule, it
        never touches general room isolation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="sprint-plan",
                content="ship the widget by Friday", monkeypatch=monkeypatch,
            )
            mem_b = await _open_at(
                path, session="room-b", monkeypatch=monkeypatch,
            )
            try:
                assert await mem_b.recall_notes("widget", sessions=None) == []
            finally:
                await mem_b.close()

    async def test_same_room_recall_returns_both_kinds(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Within one room, both a contact note and a plain note recall — the
        retirement only changes *cross-room* behaviour, never same-room."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            await _store(
                path, session="room-a", topic="contact:bob",
                content="Name: Bob. Likes Zig.", monkeypatch=monkeypatch,
            )
            await _store(
                path, session="room-a", topic="standup",
                content="standup is at 10am Zig review",
                monkeypatch=monkeypatch,
            )
            mem_a = await _open_at(
                path, session="room-a", monkeypatch=monkeypatch,
            )
            try:
                hits = await mem_a.recall_notes("Zig", sessions=None)
            finally:
                await mem_a.close()
            assert {n.topic for n in hits} == {"contact:bob", "standup"}

    async def test_recall_contact_notes_helper_removed(self) -> None:
        """The dedicated cross-room helper is gone — its sole job (cross-room
        contact recall) is now served by the relationship tier."""
        assert not hasattr(EpisodicMemory, "recall_contact_notes")


class _NotesMixin(_MemoryContextMixin):
    def __init__(self) -> None:
        super().__init__()
        self._clock = WallClock()
        self._timezone = "UTC"

    def _format_event(self, event):  # type: ignore[override]
        # A query that does NOT lexically match the contact note, to prove the
        # injection is not pulling it in via the (room-scoped) query path.
        return "rollout planning status"


class TestContactNoteNotInjectedCrossRoom:
    """``_inject_memory_context`` no longer surfaces a sender's contact note
    cross-room via the notes tier — the inverse of the retired F-3b
    injection test.  (Cross-room identity now rides the relationship tier;
    here the relationship lookup returns no summary, so nothing should leak
    through the notes path.)"""

    async def _wire(self, episodic: EpisodicMemory, sender_id: str):
        mixin = _NotesMixin()
        mixin.agent_id = "shared-agent"
        mixin._working_memory = WorkingMemory(max_tokens=8192)
        mixin._episodic_memory = episodic
        mixin._relationship_memory = AsyncMock()
        mixin._relationship_memory.get_relationship_summary.return_value = None
        mixin._relationship_memory.get_identity.return_value = None
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

    async def test_sender_contact_note_not_injected_cross_room(
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
            # Inject as if we are now in a different room.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "room-b")
            mixin, event = await self._wire(mem, "alice-uid")
            await mixin._inject_memory_context(event)
            section = mixin._working_memory.get_section("recent_notes")
            assert section is None, (
                "room-a contact note must not leak into room-b via the notes tier"
            )
        finally:
            await mem.close()
