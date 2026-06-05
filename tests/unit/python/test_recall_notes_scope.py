"""F-7 / Option A — ``recall_notes`` is scope-aware *by topic*, so the
single recall method both read paths use enforces one rule.

The seam (see ``docs/v0.3.7-f7-cross-room-recall-seam.md``): auto-injection
recalled person-keyed ``contact:*`` notes cross-room, but the LLM-facing
``recall_notes`` tool was session-scoped for everything — so an explicit
"do you remember me?" on a fresh channel hit the narrower path and
reported amnesia, while a casual turn (auto-injection) recalled the
person. Scope was decided at the call site, not by the data.

Option A makes scope a property of the note's topic inside
``NoteStore.recall_notes`` (the one method the tool *and* the injection
query path call): a ``contact:<participant>`` note recalls **cross-room**
(still principal/epoch-scoped); every other note stays room-scoped
(``session_id IN (active, legacy)``). Mutation (update/delete/count) is
deliberately unchanged — a foreign-room contact note stays read-only.

These tests pin that rule at ``recall_notes`` so the tool and the
injection query path can never diverge again.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
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


async def _open(path: str, *, session: str, monkeypatch) -> EpisodicMemory:
    monkeypatch.setenv(SESSION_ID_ENV_VAR, session)
    mem = EpisodicMemory(agent_id="shared-agent", db_path=path)
    await mem.initialize()
    return mem


class TestRecallNotesScopeByTopic:
    async def test_contact_note_recalled_cross_room(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``contact:*`` note saved in room A is returned by
        ``recall_notes`` from room B — the seam fix: the tool's recall path
        now reaches the same person identity auto-injection does.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            await _store(
                path, session="room-a", topic="contact:alice",
                content="Name: Alice. Favorite language: Rust.",
                monkeypatch=monkeypatch,
            )
            mem_b = await _open(path, session="room-b", monkeypatch=monkeypatch)
            try:
                hits = await mem_b.recall_notes("Rust")
            finally:
                await mem_b.close()
            assert any("Alice" in n.content for n in hits), (
                "contact:* note must recall cross-room via recall_notes"
            )

    async def test_room_note_stays_session_scoped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-contact (room) note saved in room A is NOT returned by
        ``recall_notes`` from room B — the cross-room widening is
        contact-only; general room isolation is untouched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            await _store(
                path, session="room-a", topic="sprint-plan",
                content="ship the widget by Friday",
                monkeypatch=monkeypatch,
            )
            mem_b = await _open(path, session="room-b", monkeypatch=monkeypatch)
            try:
                hits = await mem_b.recall_notes("widget")
            finally:
                await mem_b.close()
            assert hits == [], "room note must stay room-scoped"

    async def test_contact_and_room_notes_in_same_session_both_visible(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Within the same room, both kinds recall — the cross-room rule
        only *widens* contact notes, it never *narrows* same-room recall.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            await _store(
                path, session="room-a", topic="contact:bob",
                content="Name: Bob. Likes Zig.", monkeypatch=monkeypatch,
            )
            await _store(
                path, session="room-a", topic="standup",
                content="standup is at 10am Zig review",
                monkeypatch=monkeypatch,
            )
            mem_a = await _open(path, session="room-a", monkeypatch=monkeypatch)
            try:
                hits = await mem_a.recall_notes("Zig")
            finally:
                await mem_a.close()
            topics = {n.topic for n in hits}
            assert "contact:bob" in topics
            assert "standup" in topics

    async def test_contact_recall_still_principal_epoch_scoped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cross-room is not cross-tenant: a contact note under a different
        principal is not recalled even though the session filter is relaxed.
        """
        from agents.principal_id import PRINCIPAL_ID_ENV_VAR
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            monkeypatch.setenv(PRINCIPAL_ID_ENV_VAR, "tenant-1")
            await _store(
                path, session="room-a", topic="contact:carol",
                content="Name: Carol. Favorite language: OCaml.",
                monkeypatch=monkeypatch,
            )
            monkeypatch.setenv(PRINCIPAL_ID_ENV_VAR, "tenant-2")
            mem_b = await _open(path, session="room-b", monkeypatch=monkeypatch)
            try:
                hits = await mem_b.recall_notes("OCaml")
            finally:
                await mem_b.close()
            assert hits == [], "cross-room must not cross the principal boundary"
