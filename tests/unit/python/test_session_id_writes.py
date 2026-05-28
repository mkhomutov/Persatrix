"""
Tests for the RFC 0031 write-path ``session_id`` kwarg.

``EpisodicMemory.store_episode`` and ``RelationshipMemory.record_interaction``
accept ``session_id`` as a keyword-only argument and persist it on the
appropriate row (Phase 1).  RFC 0031 Phase 2 PR 1 brings the ``notes`` tier
to parity — ``NoteStore.store_note`` (via the ``EpisodicMemory`` delegation)
also accepts and persists ``session_id``.  The default (``"legacy"``) matches
the orchestrator-side synthetic carve-out so pre-RFC callers produce
queryable rows without ambiguity.

These tests assert the **write contract** — round-trip via direct SQLite
read — without making any recall claims; recall-side filtering lands in
the later Phase 2 PRs.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory

# ─── EpisodicMemory.store_episode ───────────────────────────


class TestStoreEpisodeSessionID:
    async def test_default_writes_legacy(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode("hello", {})
        async with memory._ensure_db().execute(
            "SELECT session_id FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy"

    async def test_explicit_session_id_round_trip(
        self, memory: EpisodicMemory,
    ):
        ep_id = await memory.store_episode(
            "hello", {}, session_id="run-a",
        )
        async with memory._ensure_db().execute(
            "SELECT session_id FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-a"

    async def test_two_sessions_coexist_at_storage_layer(
        self, memory: EpisodicMemory,
    ):
        a = await memory.store_episode("a", {}, session_id="run-a")
        b = await memory.store_episode("b", {}, session_id="run-b")
        async with memory._ensure_db().execute(
            "SELECT id, session_id FROM episodes WHERE id IN (?, ?) "
            "ORDER BY id",
            (a, b),
        ) as cursor:
            rows = await cursor.fetchall()
        by_id = {r[0]: r[1] for r in rows}
        assert by_id[a] == "run-a"
        assert by_id[b] == "run-b"


# ─── EpisodicMemory.store_note (RFC 0031 Phase 2 PR 1) ──────


class TestStoreNoteSessionID:
    async def test_default_writes_legacy(self, memory: EpisodicMemory):
        # Compare against ``LEGACY_SESSION_ID`` rather than the literal
        # ``"legacy"`` so a future rename of the carve-out constant in
        # ``agents.session_id`` (and its Go-side ``channels.DefaultSessionID``
        # counterpart) only needs to touch the leaf module — the
        # signature-default pin at ``test_default_uses_legacy_session_id_constant``
        # below uses the same style.  (PR 1 second deep-review finding #3 —
        # the new notes-tier asserts had drifted to the literal while the
        # F4 normalisation asserts in ``TestStoreNoteSessionIDNormalization``
        # used the constant; this unifies the rows added by PR 1.)
        from agents.session_id import LEGACY_SESSION_ID

        note_id = await memory.store_note("topic", "content")
        async with memory._ensure_db().execute(
            "SELECT session_id FROM notes WHERE id = ?",
            (note_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == LEGACY_SESSION_ID

    async def test_explicit_session_id_round_trip(
        self, memory: EpisodicMemory,
    ):
        note_id = await memory.store_note(
            "topic", "content", session_id="run-a",
        )
        async with memory._ensure_db().execute(
            "SELECT session_id FROM notes WHERE id = ?",
            (note_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-a"

    async def test_two_sessions_coexist_at_storage_layer(
        self, memory: EpisodicMemory,
    ):
        a = await memory.store_note("a", "ca", session_id="run-a")
        b = await memory.store_note("b", "cb", session_id="run-b")
        async with memory._ensure_db().execute(
            "SELECT id, session_id FROM notes WHERE id IN (?, ?) "
            "ORDER BY id",
            (a, b),
        ) as cursor:
            rows = await cursor.fetchall()
        by_id = {r[0]: r[1] for r in rows}
        assert by_id[a] == "run-a"
        assert by_id[b] == "run-b"


class TestStoreNoteSessionIDNormalization:
    """PR 1 review F4 + F5 — the ``session_id`` kwarg accepts the same
    shape the leaf module's ``resolve_session_id_silent`` produces:
    empty / whitespace-only strings collapse to
    :data:`agents.session_id.LEGACY_SESSION_ID`, and the kwarg default
    is the constant (not a hand-duplicated ``"legacy"`` literal) so a
    cross-language rename of the carve-out only needs to touch the
    leaf module.

    Without this normalisation a caller passing ``session_id=""``
    persists an empty string into the NOT NULL column — accepted by
    SQLite ('' is not NULL) but orphaned from both real-session and
    ``'legacy'`` carve-out filters once Phase 2 recall lands.
    """

    async def test_empty_string_normalises_to_legacy(
        self, memory: EpisodicMemory,
    ):
        from agents.session_id import LEGACY_SESSION_ID

        note_id = await memory.store_note(
            "topic", "content", session_id="",
        )
        async with memory._ensure_db().execute(
            "SELECT session_id FROM notes WHERE id = ?",
            (note_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == LEGACY_SESSION_ID

    async def test_whitespace_only_normalises_to_legacy(
        self, memory: EpisodicMemory,
    ):
        from agents.session_id import LEGACY_SESSION_ID

        note_id = await memory.store_note(
            "topic", "content", session_id="   ",
        )
        async with memory._ensure_db().execute(
            "SELECT session_id FROM notes WHERE id = ?",
            (note_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == LEGACY_SESSION_ID

    async def test_default_uses_legacy_session_id_constant(self):
        """Kwarg default on both the storage primitive and the mixin
        is the centralised constant — not a hand-duplicated literal.

        A future rename of ``LEGACY_SESSION_ID`` (or its Go-side
        ``channels.DefaultSessionID`` counterpart) only needs to touch
        the leaf module; this pin catches a regression that re-inlines
        the string literal on either signature.
        """
        import inspect

        from agents.memory.episodic_notes_api import _EpisodicNotesAPIMixin
        from agents.memory.notes import NoteStore
        from agents.session_id import LEGACY_SESSION_ID

        for fn in (NoteStore.store_note, _EpisodicNotesAPIMixin.store_note):
            sig = inspect.signature(fn)
            assert sig.parameters["session_id"].default == LEGACY_SESSION_ID, (
                f"{fn.__qualname__} session_id default drifted from "
                f"LEGACY_SESSION_ID — re-import the constant rather than "
                f"hardcoding the literal"
            )


class TestStoreNoteToolThreadsActiveSession:
    """The builtin ``store_note`` tool threads the per-process
    ``PERSATRIX_SESSION_ID`` into the write, not the bare ``"legacy"``
    default — so agent-initiated notes are tagged with the active
    operator namespace (RFC 0031 Phase 2 PR 1).  Resolution happens at
    tool-construction time, mirroring the facade's silent construction-time
    read; this pins that the threading is not silently dropped.
    """

    async def test_tool_threads_resolved_session_id(
        self, memory: EpisodicMemory, monkeypatch,
    ):
        from agents.tools.builtin import create_memory_tools
        from agents.tools.permissions import PermissionGate
        from agents.tools.registry import clear_registry, get_tool

        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-tool")
        gate = PermissionGate({"memory": {"read": True, "write": True}})
        clear_registry()
        try:
            create_memory_tools(memory, gate, max_notes=500)
            td = get_tool("store_note")
            assert td is not None
            # ``ToolDefinition.func`` is typed ``Callable | None`` on the
            # registry dataclass (the field has a ``None`` default so an
            # unregistered stub can be constructed in non-test code).  The
            # ``assert td is not None`` above narrows ``td`` but not
            # ``td.func`` — needed because this test method's typed
            # ``memory: EpisodicMemory`` parameter flips mypy into checked
            # mode (peer tests in ``test_memory_tools_permissions.py`` rely
            # on untyped signatures to skip narrowing).  The runtime
            # contract is that any tool returned by ``get_tool`` has
            # ``func`` set by the ``@tool`` decorator, so the assertion is
            # always true here.
            assert td.func is not None
            result = await td.func(topic="t", content="c")
            assert result.success is True
            note_id = result.data["note_id"]
        finally:
            clear_registry()

        async with memory._ensure_db().execute(
            "SELECT session_id FROM notes WHERE id = ?",
            (note_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-tool"


# ─── RelationshipMemory.record_interaction ──────────────────


@pytest.fixture
async def rel_memory():
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestRecordInteractionSessionID:
    async def test_default_writes_legacy(self, rel_memory: RelationshipMemory):
        iid = await rel_memory.record_interaction("bob", "chat")
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy"
        # interaction id returned; no error
        assert iid

    async def test_explicit_session_id_round_trip(
        self, rel_memory: RelationshipMemory,
    ):
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-a",
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-a"

    async def test_second_interaction_does_not_overwrite_session_id(
        self, rel_memory: RelationshipMemory,
    ):
        # The first interaction stamps the row's session_id; a later
        # interaction with a different value MUST NOT overwrite it.
        # The relationships row is a stable per-pair identity; the
        # write-path-only Phase 1 contract is that the column tracks
        # the *first-seen* session id, mirroring how
        # ``last_interaction_at`` updates while ``trust_score`` does
        # not on a bare record_interaction.  This is the per-row
        # storage tag — per-interaction session id lives on the
        # interactions table once Phase 2's recall path needs it.
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-a",
        )
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-b",
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        # Phase 1 contract: row.session_id is the *first-seen* id.
        assert row is not None
        assert row[0] == "run-a"


# ─── normalize_session_id helper (RFC 0031 Phase 2 PR 4 — F16) ─


class TestNormalizeSessionIDHelper:
    """The shared :func:`agents.session_id.normalize_session_id` helper.

    Symmetric across the four persona-memory tier write boundaries —
    every tier funnels caller-supplied ``session_id`` through this so a
    direct programmatic caller passing ``""`` / ``"   "`` / ``None`` /
    ``"  run-a  "`` lands the same canonical value as the env-var read
    via :func:`agents.session_id.resolve_session_id_silent`.
    """

    def test_empty_string_collapses_to_legacy(self) -> None:
        from agents.session_id import LEGACY_SESSION_ID, normalize_session_id

        assert normalize_session_id("") == LEGACY_SESSION_ID

    def test_whitespace_only_collapses_to_legacy(self) -> None:
        from agents.session_id import LEGACY_SESSION_ID, normalize_session_id

        assert normalize_session_id("   ") == LEGACY_SESSION_ID
        assert normalize_session_id("\t") == LEGACY_SESSION_ID
        assert normalize_session_id("\n") == LEGACY_SESSION_ID

    def test_none_collapses_to_legacy(self) -> None:
        from agents.session_id import LEGACY_SESSION_ID, normalize_session_id

        assert normalize_session_id(None) == LEGACY_SESSION_ID

    def test_surrounding_whitespace_stripped(self) -> None:
        from agents.session_id import normalize_session_id

        assert normalize_session_id("  run-a  ") == "run-a"

    def test_canonical_value_passes_through(self) -> None:
        from agents.session_id import normalize_session_id

        assert normalize_session_id("run-a") == "run-a"


class TestSessionIDNormalizationUniformAcrossTiers:
    """Every persona-memory tier write boundary normalises empty /
    whitespace input identically (RFC 0031 Phase 2 PR 4 — PR 1 F16
    carry-forward).  Without this, a tier would persist a NOT NULL
    ``""`` row that escapes both real-session and legacy-carve-out
    recall filters.
    """

    async def test_episodic_normalises_empty(
        self, memory: EpisodicMemory,
    ) -> None:
        from agents.session_id import LEGACY_SESSION_ID

        ep_id = await memory.store_episode("hello", {}, session_id="")
        async with memory._ensure_db().execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == LEGACY_SESSION_ID

    async def test_episodic_normalises_whitespace(
        self, memory: EpisodicMemory,
    ) -> None:
        from agents.session_id import LEGACY_SESSION_ID

        ep_id = await memory.store_episode("hello", {}, session_id="   ")
        async with memory._ensure_db().execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == LEGACY_SESSION_ID

    async def test_relationship_normalises_empty(
        self, rel_memory: RelationshipMemory,
    ) -> None:
        from agents.session_id import LEGACY_SESSION_ID

        await rel_memory.record_interaction("bob", "chat", session_id="")
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == LEGACY_SESSION_ID

    async def test_facts_normalises_empty(self) -> None:
        from agents.memory.facts import FactStore
        from agents.session_id import LEGACY_SESSION_ID

        store = FactStore(
            agent_id="test-agent",
            db_path=":memory:",
            predicate_validator=lambda _p: None,
        )
        await store.initialize()
        try:
            fact_id = await store.store(
                subject="alice",
                predicate="likes",
                object="kayaking",
                source_interaction_id=None,
                asserted_at=1.0,
                session_id="",
            )
            db = store._ensure_db()  # noqa: SLF001 — test inspection
            async with db.execute(
                "SELECT session_id FROM facts WHERE fact_id = ?", (fact_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == LEGACY_SESSION_ID
        finally:
            await store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
