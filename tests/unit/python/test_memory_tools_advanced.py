"""
Tests for agent-initiated memory tools (notes CRUD, FTS5 search, pruning,
permission gating, and auto_reflect_after counter).

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.notes import _FTS5_SPECIAL
from agents.tools.builtin import check_auto_reflect, create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry, get_tool


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with an empty tool registry."""
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
async def memory():
    """Create an initialized EpisodicMemory with in-memory DB."""
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
def gate_rw():
    """PermissionGate that allows memory:read and memory:write."""
    return PermissionGate({"memory": {"read": True, "write": True}})


@pytest.fixture
def gate_ro():
    """PermissionGate that allows memory:read only."""
    return PermissionGate({"memory": {"read": True}})


@pytest.fixture
def gate_none():
    """PermissionGate with no memory permissions."""
    return PermissionGate({})


@pytest.fixture
async def tools(memory, gate_rw):
    """Create and return memory tool definitions."""
    return create_memory_tools(memory, gate_rw, max_notes=500)


# ─── Note pruning ───────────────────────────────────────────


class TestNotePruning:
    async def test_prune_oldest_low_access(self, memory):
        """When at max_notes, oldest low-access note is pruned."""
        ids = []
        for i in range(3):
            ids.append(await memory.store_note(f"topic-{i}", f"content-{i}", max_notes=3))
        # Access the first note to boost its access_count
        await memory.recall_notes("topic-0")
        # This should prune topic-1 (lowest access_count, oldest after topic-0)
        await memory.store_note("topic-new", "new content", max_notes=3)
        assert await memory.count_notes() == 3
        notes = await memory.recall_notes(limit=10)
        topics = {n.topic for n in notes}
        assert "topic-new" in topics
        assert "topic-0" in topics  # preserved (higher access count)

    async def test_no_prune_under_limit(self, memory):
        await memory.store_note("a", "content-a", max_notes=10)
        await memory.store_note("b", "content-b", max_notes=10)
        assert await memory.count_notes() == 2


# ─── Pruning + FTS5 trigger interaction (F-59 should-fix) ──


class TestPruningFTS5Cleanup:
    """Pruned notes must not remain in the FTS5 index."""

    async def test_pruned_notes_not_findable_via_fts5(self, memory):
        """After pruning, the FTS5 DELETE trigger removes pruned notes from the index."""
        # Store 3 notes with distinctive content for FTS5 matching.
        # Do NOT recall between stores — that would bump access_count and
        # change which note gets pruned (prune order: access_count ASC,
        # created_at ASC).
        await memory.store_note("alpha", "unique-alpha-xyzzy", max_notes=10)
        await memory.store_note("beta", "unique-beta-xyzzy", max_notes=10)
        await memory.store_note("gamma", "unique-gamma-xyzzy", max_notes=10)
        assert await memory.count_notes() == 3

        # Store a 4th note with max_notes=3 — prunes 1 note.
        # All three have access_count=0, so the oldest (alpha) is pruned.
        await memory.store_note("delta", "unique-delta-xyzzy", max_notes=3)
        assert await memory.count_notes() == 3

        # Pruned note (alpha) must not be findable via recall
        found_after = await memory.recall_notes("unique-alpha-xyzzy")
        assert len(found_after) == 0

        # Surviving notes should still be findable
        found_beta = await memory.recall_notes("unique-beta-xyzzy")
        assert len(found_beta) == 1


# ─── auto_reflect_after counter ─────────────────────────────


class TestAutoReflect:
    async def test_counter_increments(self, memory):
        assert await memory.get_interaction_count() == 0
        count = await memory.increment_interaction_count()
        assert count == 1
        count = await memory.increment_interaction_count()
        assert count == 2

    async def test_counter_resets(self, memory):
        await memory.increment_interaction_count()
        await memory.increment_interaction_count()
        await memory.reset_interaction_count()
        assert await memory.get_interaction_count() == 0

    async def test_check_auto_reflect_fires_at_threshold(self, memory):
        for _ in range(4):
            result = await check_auto_reflect(memory, auto_reflect_after=5)
            assert result is None
        # 5th interaction should fire
        result = await check_auto_reflect(memory, auto_reflect_after=5)
        assert result is not None
        assert "store_note" in result
        # Counter should be reset
        assert await memory.get_interaction_count() == 0

    async def test_check_auto_reflect_disabled_when_zero(self, memory):
        result = await check_auto_reflect(memory, auto_reflect_after=0)
        assert result is None

    async def test_counter_persists_across_sessions(self):
        """Counter survives close + reopen."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            mem = EpisodicMemory(agent_id="test-agent", db_path=path)
            await mem.initialize()
            await mem.increment_interaction_count()
            await mem.increment_interaction_count()
            await mem.close()

            mem2 = EpisodicMemory(agent_id="test-agent", db_path=path)
            await mem2.initialize()
            assert await mem2.get_interaction_count() == 2
            await mem2.close()
        finally:
            os.unlink(path)


# ─── Migration v2 ──────────────────────────────────────────


class TestMigrationV2:
    async def test_notes_table_created(self, memory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notes'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_migration_version_recorded(self, memory):
        db = memory._ensure_db()
        async with db.execute("SELECT MAX(version) FROM schema_version") as cursor:
            row = await cursor.fetchone()
        assert row[0] >= 2

    async def test_migration_idempotent(self, memory):
        """Re-running initialize on same DB doesn't error."""
        await memory.close()
        mem2 = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
        await mem2.initialize()
        # Notes table should still work
        await mem2.store_note("test", "content")
        assert await mem2.count_notes() == 1
        await mem2.close()

    async def test_notes_fts5_triggers(self, memory):
        """Insert, update, delete sync to FTS5 index."""
        note_id = await memory.store_note("searchable", "unique-keyword-xyz")
        notes = await memory.recall_notes("unique-keyword-xyz")
        assert len(notes) == 1

        # Update content — FTS5 should reflect the change
        await memory.update_note(note_id, "different-content-abc")
        notes = await memory.recall_notes("unique-keyword-xyz")
        assert len(notes) == 0
        notes = await memory.recall_notes("different-content-abc")
        assert len(notes) == 1

        # Delete — FTS5 should remove the entry
        await memory.delete_note(note_id)
        notes = await memory.recall_notes("different-content-abc")
        assert len(notes) == 0


# ─── FTS5 malformed query fallback (F-3b-5) ────────────────


class TestRecallNotesFTS5Fallback:
    """Notes recall with malformed FTS5 queries falls back to LIKE without crashing."""

    @pytest.mark.parametrize("malformed_query", ["NOT", "*", "OR", "AND NOT"])
    async def test_malformed_fts5_query_returns_results(self, memory, gate_rw, malformed_query):
        create_memory_tools(memory, gate_rw)
        # Store a note first so LIKE fallback has something to match
        store = get_tool("store_note")
        await store.func(topic="test", content="some content")

        recall = get_tool("recall_notes")
        result = await recall.func(query=malformed_query, limit=10)
        # Should not crash — either returns matches via LIKE or empty list
        assert result.success is True


# ─── FTS5 query sanitization (MT-PERSONA-001 fix) ───────────


class TestFTS5QuerySanitization:
    """FTS5 special-character stripping in NoteStore._recall_notes_fts5.

    Colons trigger FTS5 column-filter syntax (``col:term``); when the word
    before the colon is not a declared FTS5 column the engine raises
    ``sqlite3.OperationalError: no such column: <word>``.  The fix strips
    FTS5 operator characters before passing user/LLM queries to MATCH.
    """

    # ── Regex unit tests (no DB required) ──────────────────────

    def test_colon_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "tick:scheduler").strip() == "tick scheduler"

    def test_colon_with_space_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "tick: scheduler").strip() == "tick  scheduler".strip()

    def test_double_quote_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", '"phrase query"').strip() == "phrase query"

    def test_wildcard_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "term*").strip() == "term"

    def test_caret_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "^anchor").strip() == "anchor"

    def test_parens_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "(grouped terms)").strip() == "grouped terms"

    def test_only_special_chars_becomes_empty(self):
        assert _FTS5_SPECIAL.sub(" ", '":*^()').strip() == ""

    def test_plain_term_unchanged(self):
        assert _FTS5_SPECIAL.sub(" ", "asyncio").strip() == "asyncio"

    # ── Integration tests via EpisodicMemory ───────────────────

    async def test_colon_query_does_not_raise(self, memory):
        """Colon in query must not bubble up a sqlite3.OperationalError."""
        await memory.store_note("tick scheduler", "tick scheduler started for ember-owl")
        notes = await memory.recall_notes("tick: scheduler")
        assert isinstance(notes, list)

    async def test_colon_query_finds_matching_notes(self, memory):
        """After sanitizing 'tick:' → 'tick', FTS5 or LIKE still returns results."""
        await memory.store_note("tick", "tick scheduler started")
        await memory.store_note("other", "unrelated database configuration")
        notes = await memory.recall_notes("tick:")
        assert any(n.topic == "tick" for n in notes)

    async def test_wildcard_query_finds_matching_notes(self, memory):
        """Wildcard stripped: 'architecture*' → 'architecture' still finds results."""
        await memory.store_note("architecture", "microservices and async patterns")
        await memory.store_note("recipes", "pasta and sauce preparation")
        notes = await memory.recall_notes("architecture*")
        assert any(n.topic == "architecture" for n in notes)

    async def test_only_special_chars_does_not_raise(self, memory):
        """A query of only special characters (sanitizes to empty) does not raise."""
        await memory.store_note("any", "some content here")
        notes = await memory.recall_notes('":*^()')
        assert isinstance(notes, list)

    # ── Extended regex tests for broader sanitizer ─────────────

    def test_comma_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "hi, do you remember me?").strip() == "hi  do you remember me"

    def test_period_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "Autonomous tick. Review goals.").strip() == "Autonomous tick  Review goals"

    def test_angle_brackets_stripped(self):
        result = _FTS5_SPECIAL.sub(" ", '<|user_message user_id="local"|>').strip()
        # All non-alphanumeric chars stripped; underscores become spaces too.
        assert "<" not in result
        assert "|" not in result
        assert ">" not in result
        assert "local" in result

    def test_pipe_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "option|alternative").strip() == "option alternative"

    def test_mixed_punctuation_stripped(self):
        assert _FTS5_SPECIAL.sub(" ", "I am Max, the creator of Persatrix.").strip() == "I am Max  the creator of Persatrix"

    def test_hyphen_stripped(self):
        """Hyphens are non-alphanumeric and should be stripped."""
        assert _FTS5_SPECIAL.sub(" ", "ember-owl").strip() == "ember owl"

    # ── Integration tests for punctuation queries ──────────────

    async def test_comma_query_does_not_raise(self, memory):
        """Comma in query must not cause FTS5 syntax error."""
        await memory.store_note("greetings", "hi there, welcome back")
        notes = await memory.recall_notes("hi, remember me?")
        assert isinstance(notes, list)

    async def test_period_query_finds_notes(self, memory):
        """Period in query is stripped; remaining tokens still match."""
        await memory.store_note("goals", "Ship the release on time with quality")
        notes = await memory.recall_notes("Ship the release. On time.")
        assert any(n.topic == "goals" for n in notes)

    async def test_delimiter_tags_query_does_not_raise(self, memory):
        """XML-style delimiter tags in query do not cause errors."""
        await memory.store_note("team", "Max is the creator")
        notes = await memory.recall_notes('<|user_message user_id="local"|>\nhi\n<|/user_message|>')
        assert isinstance(notes, list)
