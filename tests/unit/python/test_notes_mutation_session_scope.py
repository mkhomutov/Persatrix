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
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.session_id import SESSION_ID_ENV_VAR


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
        ok = await memory_at_run_a._note_store.update_note(legacy_id, "edited")
        assert ok is True
