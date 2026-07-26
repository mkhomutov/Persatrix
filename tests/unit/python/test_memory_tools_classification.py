"""RFC 0037 §C/§D notes leg at the tool boundary (PR 4, review item 6).

The note tools are a stamped write choke point and a gated read surface:

* ``store_note`` stamps the acting turn's classification through rule (a)
  (absent → ``internal``, never ``public``);
* ``recall_notes`` applies the acting level's injectable-level IN-list at
  the query — an above-``L`` note neither returns nor burns a ``limit``
  slot, and a corrupted stored label falls out of the predicate (rule
  (c) in SQL);
* ``update_note`` re-stamps to ``max(existing, acting L)`` — an edit
  never lowers a note's level.

The acting level rides the task-local :mod:`agents.acting_classification`
axis, bound here exactly as ``request_scope_from_metadata`` binds it in
production (from the §B wire metadata key) — never from a tool argument.
"""

from __future__ import annotations

import pytest

from agents.acting_classification import (
    acting_classification_scope_from_metadata,
)
from agents.channel_event_classification import (
    CHANNEL_CLASSIFICATION_METADATA_KEY,
)
from agents.memory.episodic import EpisodicMemory
from agents.tools.memory_tools import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry, get_tool

# ─── Fixtures (the test_memory_tools_permissions.py harness) ────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
async def memory():
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
async def tools(memory):
    gate = PermissionGate({"memory": {"read": True, "write": True}})
    return create_memory_tools(memory, gate, max_notes=500)


def _acting(level: str):
    """Bind the acting classification the way production does — from the
    §B wire metadata key, task-locally for the turn."""
    return acting_classification_scope_from_metadata(
        {CHANNEL_CLASSIFICATION_METADATA_KEY: level},
    )


async def _level_of(memory: EpisodicMemory, note_id: str) -> str:
    async with memory._ensure_db().execute(
        "SELECT protection_level FROM notes WHERE id = ?", (note_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


# ─── store_note stamps the acting level (rule (a)) ──────────────────────


class TestStoreNoteStamp:
    async def test_stamps_acting_level(self, memory, tools):
        td = get_tool("store_note")
        with _acting("restricted"):
            result = await td.func(topic="t", content="c")
        assert result.success is True
        assert await _level_of(memory, result.data["note_id"]) == "restricted"

    async def test_unbound_turn_stamps_internal_never_public(
        self, memory, tools,
    ):
        """Rule (a) at the stamp site: a tick/CLI/pre-classification turn
        labels ``internal`` — confidential-by-default."""
        td = get_tool("store_note")
        result = await td.func(topic="t", content="c")
        assert result.success is True
        assert await _level_of(memory, result.data["note_id"]) == "internal"


# ─── recall_notes gated at the query (§D read surfaces) ─────────────────


class TestRecallNotesGated:
    async def test_above_level_note_withheld(self, memory, tools):
        """A note learned in a ``restricted`` turn never surfaces through
        the tool in an ``internal`` turn — the review-item-6 bypass."""
        store = get_tool("store_note")
        with _acting("restricted"):
            await store.func(topic="secret-plan", content="cancel project x")
        recall = get_tool("recall_notes")
        with _acting("internal"):
            result = await recall.func(query="project")
        assert result.success is True
        assert result.data == []

    async def test_at_and_below_level_notes_return(self, memory, tools):
        store = get_tool("store_note")
        with _acting("internal"):
            await store.func(topic="plan", content="project x kickoff")
        recall = get_tool("recall_notes")
        with _acting("restricted"):
            result = await recall.func(query="project")
        assert result.success is True
        assert [n["topic"] for n in result.data] == ["plan"]

    async def test_unbound_turn_floors_to_public(self, memory, tools):
        """Rule (b) on the read side: no acting scope → only ``public``
        notes return (inject less, never the ``internal`` default)."""
        await memory.store_note("p", "public content", protection_level="public")
        await memory.store_note("i", "internal content")
        recall = get_tool("recall_notes")
        result = await recall.func(query="content")
        assert result.success is True
        assert [n["topic"] for n in result.data] == ["p"]

    async def test_corrupted_label_withheld_in_sql(self, memory, tools):
        """Rule (c) realised by the IN-list: an out-of-vocabulary stored
        label falls out of the predicate even acting ``secret``."""
        note_id = await memory.store_note(
            "bad", "corrupted label content", protection_level="xyzzy",
        )
        assert note_id
        recall = get_tool("recall_notes")
        with _acting("secret"):
            result = await recall.func(query="corrupted")
        assert result.success is True
        assert result.data == []

    async def test_withheld_notes_do_not_burn_limit_slots(
        self, memory, tools,
    ):
        """SQL-side gating means ``limit`` counts visible notes — five
        protected rows cannot crowd out the one injectable row."""
        for i in range(5):
            await memory.store_note(
                f"r{i}", f"gated content {i}", protection_level="secret",
            )
        await memory.store_note(
            "open", "gated content visible", protection_level="public",
        )
        recall = get_tool("recall_notes")
        result = await recall.func(query="gated content", limit=3)
        assert result.success is True
        assert [n["topic"] for n in result.data] == ["open"]


# ─── update_note re-stamps upward only (§C item 6) ──────────────────────


class TestUpdateNoteRestamp:
    async def test_edit_raises_level_to_acting(self, memory, tools):
        store = get_tool("store_note")
        with _acting("internal"):
            stored = await store.func(topic="t", content="v1")
        note_id = stored.data["note_id"]
        update = get_tool("update_note")
        with _acting("secret"):
            result = await update.func(note_id=note_id, content="v2")
        assert result.success is True
        assert await _level_of(memory, note_id) == "secret"

    async def test_edit_never_lowers_level(self, memory, tools):
        store = get_tool("store_note")
        with _acting("secret"):
            stored = await store.func(topic="t", content="v1")
        note_id = stored.data["note_id"]
        update = get_tool("update_note")
        with _acting("public"):
            result = await update.func(note_id=note_id, content="v2")
        assert result.success is True
        assert await _level_of(memory, note_id) == "secret"

    async def test_unbound_edit_keeps_higher_level(self, memory, tools):
        """An unbound turn's stamp is ``internal`` (rule (a)); an existing
        ``restricted`` note keeps its level through the edit."""
        note_id = await memory.store_note(
            "t", "v1", protection_level="restricted",
        )
        update = get_tool("update_note")
        result = await update.func(note_id=note_id, content="v2")
        assert result.success is True
        assert await _level_of(memory, note_id) == "restricted"

    async def test_edit_never_launders_corrupted_label(self, memory, tools):
        """A corrupted existing label is outside every ``restamp_below``
        set: the edit keeps it, and it stays failing closed at read time
        (rule (c)) instead of being coerced onto the lattice."""
        note_id = await memory.store_note(
            "t", "v1", protection_level="xyzzy",
        )
        update = get_tool("update_note")
        with _acting("internal"):
            result = await update.func(note_id=note_id, content="v2")
        assert result.success is True
        assert await _level_of(memory, note_id) == "xyzzy"
