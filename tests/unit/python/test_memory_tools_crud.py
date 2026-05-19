"""
Tests for agent-initiated memory tools (notes CRUD, FTS5 search, pruning,
permission gating, and auto_reflect_after counter).

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""


import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.notes import _MAX_NOTE_CONTENT_BYTES, Note
from agents.tools.builtin import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry

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


# ─── max_notes validation (F-59-1) ─────────────────────────


class TestMaxNotesValidation:
    """store_note() rejects max_notes < 1 at the public API boundary."""

    async def test_max_notes_zero_raises(self, memory):
        with pytest.raises(ValueError, match="max_notes must be >= 1"):
            await memory.store_note("topic", "content", max_notes=0)

    async def test_max_notes_negative_raises(self, memory):
        with pytest.raises(ValueError, match="max_notes must be >= 1"):
            await memory.store_note("topic", "content", max_notes=-1)

    async def test_max_notes_one_keeps_only_newest(self, memory):
        """max_notes=1 prunes all existing notes, keeping only the newest."""
        await memory.store_note("first", "content-1", max_notes=10)
        await memory.store_note("second", "content-2", max_notes=1)
        assert await memory.count_notes() == 1
        notes = await memory.recall_notes(limit=10)
        assert notes[0].topic == "second"
