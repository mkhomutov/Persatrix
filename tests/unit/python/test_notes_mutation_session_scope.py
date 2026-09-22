"""RFC 0031 Phase 2 PR 5 / ISSUE-0077 — notes mutation surface is
session-scoped.

PR 2 closed F-3 on :meth:`NoteStore.recall_notes`; PR 5 closes the
sibling mutation surface so a caller operating under ``run-b`` cannot
mutate or delete a ``run-a`` row even if it learns the UUID through
another path.  Tracked as `ISSUE-0077
<../../../docs/issues/ISSUE-0077-notes-mutation-not-session-scoped.md>`_.

These tests live in their own file to keep
:mod:`tests.unit.python.test_episodic_session_scope` under the
500-line review-friendly cap; they share the
``memory_at_run_a`` fixture pattern documented in that module via
import.

The last two classes pin *which* session is active (`ISSUE-0164
<../../../docs/issues/ISSUE-0164-persona-cannot-edit-or-delete-its-channel-notes.md>`_).
The orchestrator gives each persona one session per channel, and the
persona runtime binds it with ``session_scope`` for every event, while
the note store keeps the ``PERSATRIX_SESSION_ID`` snapshot it was built
with at start-up.  The ``store_note`` tool and ``recall_notes`` use the
bound session; the three mutation methods must use it too, falling back
to the snapshot only when none is bound.  The tests above vary only the
env var, so the two were always the same session there.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.notes import NoteStore
from agents.session_id import LEGACY_SESSION_ID, SESSION_ID_ENV_VAR, session_scope
from agents.tools.memory_tools import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import ToolDefinition, ToolResult, get_tool

# The session the note store is built with at start-up, and two
# channels' sessions as the runtime binds them per event.
_BOOT_SESSION = "run-boot"
_SESSION = "sess-abc"
_OTHER_SESSION = "sess-xyz"


@pytest.fixture
async def memory_at_run_a(monkeypatch: pytest.MonkeyPatch):
    """``EpisodicMemory`` constructed with ``PERSATRIX_SESSION_ID=run-a``.

    Sibling of the fixture in
    :mod:`tests.unit.python.test_episodic_session_scope` — kept local
    here rather than imported so a ruff F811 fixture-name shadow does
    not trip on the import + parameter-name pair.
    """
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestNotesMutationIsSessionScoped:
    """``NoteStore.update_note`` / ``delete_note`` / ``count_notes``
    filter by ``(agent_id, session_id IN (active, legacy))``.

    The legacy carve-out matches the recall surface (a ``legacy`` row
    is mutable from every session for symmetry — permissive policy per
    ISSUE-0077 proposed fix §2).
    """

    async def test_update_note_does_not_mutate_foreign_session_row(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "memory.db")
            # Write a row under run-a.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            mem_a = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_a.initialize()
            try:
                run_a_id = await mem_a.store_note(
                    "topic-a", "original-a", session_id="run-a",
                )
            finally:
                await mem_a.close()

            # Re-open under run-b on the same DB; attempt to mutate the
            # run-a row's UUID.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            mem_b = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_b.initialize()
            try:
                assert mem_b._note_store is not None
                ok = await mem_b._note_store.update_note(run_a_id, "tampered")
                # Pre-fix: True (mutated). Post-fix: False (no row matched).
                assert ok is False
            finally:
                await mem_b.close()

            # Verify the row still carries its original content from run-a.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            mem_a2 = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_a2.initialize()
            try:
                notes = await mem_a2.recall_notes("", limit=10)
                got = next(n for n in notes if n.id == run_a_id)
                assert got.content == "original-a"
            finally:
                await mem_a2.close()

    async def test_delete_note_does_not_delete_foreign_session_row(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "memory.db")
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            mem_a = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_a.initialize()
            try:
                run_a_id = await mem_a.store_note(
                    "topic-a", "content-a", session_id="run-a",
                )
            finally:
                await mem_a.close()

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            mem_b = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_b.initialize()
            try:
                assert mem_b._note_store is not None
                ok = await mem_b._note_store.delete_note(run_a_id)
                assert ok is False
            finally:
                await mem_b.close()

            # The row must still be visible from run-a.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            mem_a2 = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_a2.initialize()
            try:
                notes = await mem_a2.recall_notes("", limit=10)
                assert any(n.id == run_a_id for n in notes)
            finally:
                await mem_a2.close()

    async def test_count_notes_is_per_session_plus_legacy(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        # Three rows across run-a / run-b / legacy on the same agent.
        await memory_at_run_a.store_note(
            "topic-a", "content-a", session_id="run-a",
        )
        await memory_at_run_a.store_note(
            "topic-b", "content-b", session_id="run-b",
        )
        await memory_at_run_a.store_note(
            "topic-l", "content-l", session_id="legacy",
        )
        # Pre-fix: 3 (agent-wide). Post-fix: 2 (active session + legacy).
        assert memory_at_run_a._note_store is not None
        n = await memory_at_run_a._note_store.count_notes()
        assert n == 2, f"count_notes leaked run-b row: got {n}"

    async def test_update_legacy_row_succeeds_from_any_session(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        """Symmetric with recall: the ``legacy`` carve-out is mutable
        from every session.  Permissive policy per ISSUE-0077 §2.
        """
        legacy_id = await memory_at_run_a.store_note(
            "topic-l", "original-l", session_id="legacy",
        )
        assert memory_at_run_a._note_store is not None
        ok = await memory_at_run_a._note_store.update_note(legacy_id, "edited")
        assert ok is True


# ─── ISSUE-0164: the active session is the bound one, read at call time ─


@pytest.fixture
async def memory_at_boot(monkeypatch: pytest.MonkeyPatch):
    """``EpisodicMemory`` built under ``PERSATRIX_SESSION_ID=run-boot``
    with no session bound, the way the persona runtime builds it once at
    start-up."""
    monkeypatch.setenv(SESSION_ID_ENV_VAR, _BOOT_SESSION)
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
def notes(memory_at_boot: EpisodicMemory) -> NoteStore:
    return memory_at_boot._ensure_note_store()


class TestNotesMutationFollowsTheBoundSession:
    """``update_note`` / ``delete_note`` / ``count_notes`` reach the notes
    ``recall_notes`` returns under the same session, plus the ``legacy``
    carve-out: the bound session when there is one, the start-up snapshot
    when there is not.
    """

    async def test_update_in_the_session_that_stored_it(
        self, notes: NoteStore,
    ) -> None:
        with session_scope(_SESSION):
            note_id = await notes.store_note("t", "original", session_id=_SESSION)
            # Positive control: the same session recalls the note.
            assert [n.id for n in await notes.recall_notes("")] == [note_id]
            assert await notes.update_note(note_id, "edited") is True
            got = await notes.recall_notes("")
        assert [n.content for n in got] == ["edited"]

    async def test_delete_in_the_session_that_stored_it(
        self, notes: NoteStore,
    ) -> None:
        with session_scope(_SESSION):
            note_id = await notes.store_note("t", "original", session_id=_SESSION)
            assert await notes.delete_note(note_id) is True
            assert await notes.recall_notes("") == []

    async def test_count_follows_the_bound_session(self, notes: NoteStore) -> None:
        # A different number of notes per session, so each total shows
        # which session was counted: boot 1, sess-abc 2, legacy 1, sess-xyz 0.
        await notes.store_note("t", "boot", session_id=_BOOT_SESSION)
        await notes.store_note("t", "abc 1", session_id=_SESSION)
        await notes.store_note("t", "abc 2", session_id=_SESSION)
        await notes.store_note("t", "legacy", session_id=LEGACY_SESSION_ID)
        with session_scope(_SESSION):
            assert await notes.count_notes() == 3
        with session_scope(_OTHER_SESSION):
            assert await notes.count_notes() == 1
        # Nothing bound: the start-up snapshot is the active session.
        assert await notes.count_notes() == 2

    async def test_other_session_cannot_touch_it(self, notes: NoteStore) -> None:
        with session_scope(_SESSION):
            note_id = await notes.store_note("t", "original", session_id=_SESSION)
        with session_scope(_OTHER_SESSION):
            assert await notes.update_note(note_id, "tampered") is False
            assert await notes.delete_note(note_id) is False
            assert await notes.count_notes() == 0
        with session_scope(_SESSION):
            got = await notes.recall_notes("")
        assert [n.content for n in got] == ["original"]

    async def test_boot_note_is_not_mutable_from_a_bound_session(
        self, notes: NoteStore,
    ) -> None:
        """The reverse: a channel's session does not recall a note tagged
        with the start-up session, so it may not edit or delete it
        either."""
        note_id = await notes.store_note("t", "original", session_id=_BOOT_SESSION)
        with session_scope(_SESSION):
            assert await notes.recall_notes("") == []
            assert await notes.update_note(note_id, "tampered") is False
            assert await notes.delete_note(note_id) is False
        # With nothing bound, the start-up session still owns it.
        assert await notes.update_note(note_id, "edited") is True

    async def test_legacy_note_stays_mutable_from_a_bound_session(
        self, notes: NoteStore,
    ) -> None:
        """Every session recalls a ``legacy`` note, so every session may
        edit and delete it — the carve-out is unchanged."""
        note_id = await notes.store_note(
            "t", "original", session_id=LEGACY_SESSION_ID,
        )
        with session_scope(_SESSION):
            assert await notes.update_note(note_id, "edited") is True
            assert await notes.delete_note(note_id) is True


async def _run_tool(name: str, **kwargs: str) -> ToolResult:
    """Call a registered note tool the way the tool loop does."""
    td = get_tool(name)
    assert td is not None and td.func is not None, name
    result: ToolResult = await td.func(**kwargs)
    return result


class TestNoteToolsInAChannelSession:
    """The persona path end to end.  ``store_note`` tags a note with the
    channel's bound session, so ``update_note`` and ``delete_note`` must
    find it under that session, and answer "Note not found" under any
    other.
    """

    @pytest.fixture(autouse=True)
    def tools(self, memory_at_boot: EpisodicMemory) -> list[ToolDefinition]:
        # Built with nothing bound, as the runtime builds them at start-up.
        gate = PermissionGate({"memory": {"read": True, "write": True}})
        return create_memory_tools(memory_at_boot, gate)

    @staticmethod
    async def _store_under_session() -> str:
        with session_scope(_SESSION):
            stored = await _run_tool("store_note", topic="t", content="v1")
        assert stored.success is True, stored.error
        note_id: str = stored.data["note_id"]
        return note_id

    async def test_same_session_edits_and_deletes_its_note(self) -> None:
        note_id = await self._store_under_session()
        with session_scope(_SESSION):
            updated = await _run_tool("update_note", note_id=note_id, content="v2")
            deleted = await _run_tool("delete_note", note_id=note_id)
        assert updated.success is True, updated.error
        assert deleted.success is True, deleted.error

    async def test_other_session_gets_note_not_found(self) -> None:
        note_id = await self._store_under_session()
        with session_scope(_OTHER_SESSION):
            updated = await _run_tool("update_note", note_id=note_id, content="v2")
            deleted = await _run_tool("delete_note", note_id=note_id)
        assert updated.error == f"Note not found: {note_id}"
        assert deleted.error == f"Note not found: {note_id}"
