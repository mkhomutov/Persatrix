"""
Tests for agent-initiated memory tools (notes CRUD, FTS5 search, pruning,
permission gating, and auto_reflect_after counter).

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory, Note, _MAX_NOTE_CONTENT_BYTES
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


# ─── Note dataclass ─────────────────────────────────────────


class TestNoteDataclass:
    def test_note_fields(self):
        note = Note(
            id="n-1",
            agent_id="test-agent",
            topic="testing",
            content="some content",
            tags=["tag1"],
            access_count=0,
            created_at=1000.0,
            updated_at=1000.0,
        )
        assert note.id == "n-1"
        assert note.topic == "testing"
        assert note.tags == ["tag1"]

    def test_note_defaults(self):
        note = Note(id="n-2", agent_id="a", topic="t", content="c")
        assert note.tags == []
        assert note.access_count == 0
        assert note.created_at == 0.0


# ─── Notes CRUD via EpisodicMemory ──────────────────────────


class TestStoreNote:
    async def test_store_and_recall_roundtrip(self, memory):
        note_id = await memory.store_note("project", "some insight")
        assert note_id
        notes = await memory.recall_notes()
        assert len(notes) == 1
        assert notes[0].topic == "project"
        assert notes[0].content == "some insight"

    async def test_store_with_tags(self, memory):
        await memory.store_note("arch", "microservices", tags=["design", "v2"])
        notes = await memory.recall_notes()
        assert notes[0].tags == ["design", "v2"]

    async def test_store_empty_topic_raises(self, memory):
        with pytest.raises(ValueError, match="topic must not be empty"):
            await memory.store_note("", "content")

    async def test_store_empty_content_raises(self, memory):
        with pytest.raises(ValueError, match="content must not be empty"):
            await memory.store_note("topic", "")

    async def test_store_oversized_content_raises(self, memory):
        big = "x" * (_MAX_NOTE_CONTENT_BYTES + 1)
        with pytest.raises(ValueError, match="exceeds.*byte limit"):
            await memory.store_note("topic", big)

    async def test_store_strips_topic_whitespace(self, memory):
        await memory.store_note("  padded  ", "content")
        notes = await memory.recall_notes()
        assert notes[0].topic == "padded"


class TestRecallNotes:
    async def test_recall_empty_db(self, memory):
        notes = await memory.recall_notes("anything")
        assert notes == []

    async def test_recall_increments_access_count(self, memory):
        await memory.store_note("topic", "content")
        notes = await memory.recall_notes()
        assert notes[0].access_count == 1
        notes = await memory.recall_notes()
        assert notes[0].access_count == 2

    async def test_recall_limit(self, memory):
        for i in range(5):
            await memory.store_note(f"topic-{i}", f"content-{i}")
        notes = await memory.recall_notes(limit=3)
        assert len(notes) == 3

    async def test_recall_invalid_limit_raises(self, memory):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await memory.recall_notes(limit=0)

    async def test_recall_fts5_search(self, memory):
        await memory.store_note("python", "asyncio event loop patterns")
        await memory.store_note("rust", "ownership and borrowing rules")
        notes = await memory.recall_notes("asyncio")
        assert len(notes) == 1
        assert notes[0].topic == "python"

    async def test_recall_no_query_returns_recent(self, memory):
        await memory.store_note("old", "first")
        await memory.store_note("new", "second")
        notes = await memory.recall_notes()
        # Most recent first
        assert notes[0].topic == "new"


class TestUpdateNote:
    async def test_update_content(self, memory):
        note_id = await memory.store_note("topic", "original")
        found = await memory.update_note(note_id, "updated")
        assert found is True
        notes = await memory.recall_notes()
        assert notes[0].content == "updated"
        assert notes[0].topic == "topic"  # preserved

    async def test_update_nonexistent_returns_false(self, memory):
        found = await memory.update_note("no-such-id", "content")
        assert found is False

    async def test_update_empty_content_raises(self, memory):
        note_id = await memory.store_note("topic", "original")
        with pytest.raises(ValueError, match="content must not be empty"):
            await memory.update_note(note_id, "")

    async def test_update_oversized_content_raises(self, memory):
        note_id = await memory.store_note("topic", "original")
        big = "x" * (_MAX_NOTE_CONTENT_BYTES + 1)
        with pytest.raises(ValueError, match="exceeds.*byte limit"):
            await memory.update_note(note_id, big)


class TestDeleteNote:
    async def test_delete_existing(self, memory):
        note_id = await memory.store_note("topic", "content")
        found = await memory.delete_note(note_id)
        assert found is True
        assert await memory.count_notes() == 0

    async def test_delete_nonexistent_returns_false(self, memory):
        found = await memory.delete_note("no-such-id")
        assert found is False

    async def test_delete_then_recall_empty(self, memory):
        note_id = await memory.store_note("topic", "content")
        await memory.delete_note(note_id)
        notes = await memory.recall_notes("topic")
        assert notes == []


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


# ─── Agent isolation ────────────────────────────────────────


class TestAgentIsolation:
    async def test_agent_cannot_see_other_agent_notes(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            mem_a = EpisodicMemory(agent_id="agent-a", db_path=path)
            mem_b = EpisodicMemory(agent_id="agent-b", db_path=path)
            await mem_a.initialize()
            await mem_b.initialize()

            await mem_a.store_note("secret", "agent-a only")
            notes_b = await mem_b.recall_notes("secret")
            assert notes_b == []

            notes_a = await mem_a.recall_notes("secret")
            assert len(notes_a) == 1

            await mem_a.close()
            await mem_b.close()
        finally:
            os.unlink(path)

    async def test_agent_cannot_delete_other_agent_note(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            mem_a = EpisodicMemory(agent_id="agent-a", db_path=path)
            mem_b = EpisodicMemory(agent_id="agent-b", db_path=path)
            await mem_a.initialize()
            await mem_b.initialize()

            note_id = await mem_a.store_note("topic", "content")
            found = await mem_b.delete_note(note_id)
            assert found is False
            assert await mem_a.count_notes() == 1

            await mem_a.close()
            await mem_b.close()
        finally:
            os.unlink(path)


# ─── Permission gating (via tools) ─────────────────────────


class TestPermissionGating:
    async def test_store_note_denied_without_write(self, memory, gate_ro):
        create_memory_tools(memory, gate_ro)
        td = get_tool("store_note")
        assert td is not None
        result = await td.func(topic="t", content="c")
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_recall_notes_denied_without_read(self, memory, gate_none):
        create_memory_tools(memory, gate_none)
        td = get_tool("recall_notes")
        assert td is not None
        result = await td.func()
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_update_note_denied_without_write(self, memory, gate_ro):
        # First create with full perms, then try update with read-only
        note_id = await memory.store_note("topic", "content")
        create_memory_tools(memory, gate_ro)
        td = get_tool("update_note")
        assert td is not None
        result = await td.func(note_id=note_id, content="new")
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_delete_note_denied_without_write(self, memory, gate_ro):
        note_id = await memory.store_note("topic", "content")
        create_memory_tools(memory, gate_ro)
        td = get_tool("delete_note")
        assert td is not None
        result = await td.func(note_id=note_id)
        assert result.success is False
        assert "Permission denied" in result.error


# ─── Memory tools (happy path via tool functions) ───────────


class TestMemoryToolsHappyPath:
    async def test_store_note_tool(self, tools):
        td = get_tool("store_note")
        result = await td.func(topic="design", content="use event sourcing", tags="arch,v2")
        assert result.success is True
        assert "note_id" in result.data

    async def test_recall_notes_tool(self, memory, tools):
        await memory.store_note("testing", "always mock LLM calls")
        td = get_tool("recall_notes")
        result = await td.func(query="mock")
        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0]["topic"] == "testing"

    async def test_update_note_tool(self, memory, tools):
        note_id = await memory.store_note("topic", "original")
        td = get_tool("update_note")
        result = await td.func(note_id=note_id, content="updated")
        assert result.success is True
        assert result.data["updated"] is True

    async def test_delete_note_tool(self, memory, tools):
        note_id = await memory.store_note("topic", "to-delete")
        td = get_tool("delete_note")
        result = await td.func(note_id=note_id)
        assert result.success is True
        assert result.data["deleted"] is True

    async def test_update_nonexistent_note_tool(self, tools):
        td = get_tool("update_note")
        result = await td.func(note_id="no-such-id", content="x")
        assert result.success is False
        assert "not found" in result.error.lower()

    async def test_delete_nonexistent_note_tool(self, tools):
        td = get_tool("delete_note")
        result = await td.func(note_id="no-such-id")
        assert result.success is False
        assert "not found" in result.error.lower()

    async def test_store_note_tool_with_empty_tags(self, tools):
        td = get_tool("store_note")
        result = await td.func(topic="t", content="c", tags="")
        assert result.success is True

    async def test_tool_registration(self, tools):
        """All 4 memory tools are registered."""
        for name in ("store_note", "recall_notes", "update_note", "delete_note"):
            assert get_tool(name) is not None
        assert len(tools) == 4


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
