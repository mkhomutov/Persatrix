"""
Tests for agent-initiated memory tools (notes CRUD, FTS5 search, pruning,
permission gating, and auto_reflect_after counter).

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.tools.builtin import create_memory_tools
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
        result = await td.func(note_id="00000000-0000-0000-0000-000000000000", content="x")
        assert result.success is False
        assert "not found" in result.error.lower()

    async def test_delete_nonexistent_note_tool(self, tools):
        td = get_tool("delete_note")
        result = await td.func(note_id="00000000-0000-0000-0000-000000000000")
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


# ─── note_id UUID validation (F-3b-3) ──────────────────────


class TestNoteIdValidation:
    """update_note and delete_note reject malformed note_id before DB round-trip."""

    async def test_update_note_rejects_non_uuid(self, memory, gate_rw):
        create_memory_tools(memory, gate_rw)
        update = get_tool("update_note")
        result = await update.func(note_id="not-a-uuid", content="new content")
        assert result.success is False
        assert "Invalid note_id" in result.error

    async def test_delete_note_rejects_non_uuid(self, memory, gate_rw):
        create_memory_tools(memory, gate_rw)
        delete = get_tool("delete_note")
        result = await delete.func(note_id="bogus-id-here")
        assert result.success is False
        assert "Invalid note_id" in result.error

    async def test_update_note_accepts_valid_uuid(self, memory, gate_rw):
        create_memory_tools(memory, gate_rw)
        store = get_tool("store_note")
        store_result = await store.func(topic="test", content="original")
        note_id = store_result.data["note_id"]

        update = get_tool("update_note")
        result = await update.func(note_id=note_id, content="updated")
        assert result.success is True


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
