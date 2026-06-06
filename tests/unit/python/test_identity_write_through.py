"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D2** — the
``store_note(contact:<id>)`` → ``upsert_identity`` write-through.

D2 routes the contact note the model already writes onto the cross-room
relationship tier (dual-write: the legacy room-scoped note is still
written during the D2 transition; D3 drops it).  This file covers:

* the pure :func:`agents.memory.identity_parse.parse_identity_fields`
  structuring step (keyed / natural / unkeyed-to-``raw``);
* the :data:`agents.sender_type` task-local participant-type binding;
* the write-through at the ``store_note`` tool boundary — identity lands
  on the relationship row with the bound participant type, the note is
  still written (dual-write), non-contact topics are untouched, and an
  absent relationship handle / a write failure never breaks the note tool;
* merge/supersede across two notes via the parser + ``merge_identity``.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.identity_parse import parse_identity_fields
from agents.memory.relationship import RelationshipMemory
from agents.sender_type import (
    current_sender_type,
    sender_type_scope_from_metadata,
)
from agents.tools.builtin import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
async def episodic():
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
async def relationship():
    rel = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await rel.initialize()
    yield rel
    await rel.close()


@pytest.fixture
def gate_rw():
    return PermissionGate({"memory": {"read": True, "write": True}})


def _store_note(tools):
    return next(td for td in tools if td.name == "store_note")


# ─── Pure parser ────────────────────────────────────────────


class TestParseIdentityFields:
    def test_keyed_name_role_prefs(self):
        out = parse_identity_fields(
            "Name: Max. Role: engineer. Favorite language: Rust.",
        )
        assert out == {
            "name": "Max",
            "role": "engineer",
            "prefs": ["Rust"],
        }

    def test_prefs_list_splits_on_comma_and_and(self):
        out = parse_identity_fields("Likes: Rust, Go and Python")
        assert out == {"prefs": ["Rust", "Go", "Python"]}

    def test_natural_name_phrasing(self):
        assert parse_identity_fields("My name is Alice") == {"name": "Alice"}
        assert parse_identity_fields("call me Bob") == {"name": "Bob"}

    def test_unkeyed_detail_preserved_as_raw(self):
        out = parse_identity_fields("Name: Max. Lives in Berlin")
        assert out["name"] == "Max"
        assert out["raw"] == "Lives in Berlin"

    def test_unknown_key_kept_verbatim_in_raw(self):
        out = parse_identity_fields("Mood: cheerful")
        assert out == {"raw": "Mood: cheerful"}

    def test_empty_input_is_empty_dict(self):
        assert parse_identity_fields("   ") == {}

    def test_equals_separator_and_case_insensitive_key(self):
        assert parse_identity_fields("ROLE = pilot") == {"role": "pilot"}


# ─── Sender-type binding ────────────────────────────────────


class TestSenderTypeScope:
    def test_default_is_agent(self):
        assert current_sender_type() == "agent"

    def test_binds_from_metadata(self):
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            assert current_sender_type() == "user"
        assert current_sender_type() == "agent"

    def test_absent_key_is_noop(self):
        with sender_type_scope_from_metadata({}):
            assert current_sender_type() == "agent"


# ─── Write-through at the tool boundary ─────────────────────


class TestIdentityWriteThrough:
    async def test_contact_note_upserts_identity_with_sender_type(
        self, episodic, relationship, gate_rw,
    ):
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)

        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            result = await store_note.func(
                topic="contact:user-alice",
                content="Name: Alice. Role: engineer.",
            )
        assert result.success

        # Identity landed on the relationship tier with the bound "user"
        # participant type — the row the recall side will later query.
        identity = await relationship.get_identity(
            "user-alice", other_participant_type="user",
        )
        assert identity == {"name": "Alice", "role": "engineer"}

    async def test_dual_write_note_still_stored(
        self, episodic, relationship, gate_rw,
    ):
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            await store_note.func(
                topic="contact:user-alice", content="Name: Alice.",
            )
        # The legacy room-scoped note is still written during the D2
        # transition (retired in D3).
        notes = await episodic.recall_notes("Alice", limit=10)
        assert any("Alice" in n.content for n in notes)

    async def test_non_contact_topic_writes_no_identity(
        self, episodic, relationship, gate_rw,
    ):
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            await store_note.func(topic="project:rust", content="Name: Alice.")
        identity = await relationship.get_identity(
            "user-alice", other_participant_type="user",
        )
        assert identity is None

    async def test_no_relationship_handle_note_tool_unaffected(
        self, episodic, gate_rw,
    ):
        # Constructed without a relationship handle (pre-wiring / non-persona
        # callers) — the note tool must still work, no crash.
        tools = create_memory_tools(episodic, gate_rw)
        store_note = _store_note(tools)
        result = await store_note.func(
            topic="contact:user-alice", content="Name: Alice.",
        )
        assert result.success

    async def test_identity_write_failure_never_breaks_note_tool(
        self, episodic, relationship, gate_rw, monkeypatch,
    ):
        async def _boom(*args, **kwargs):
            raise RuntimeError("identity backend down")

        monkeypatch.setattr(relationship, "upsert_identity", _boom)
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        result = await store_note.func(
            topic="contact:user-alice", content="Name: Alice.",
        )
        # The note write succeeded; the identity failure was swallowed.
        assert result.success

    async def test_merge_supersede_across_two_notes(
        self, episodic, relationship, gate_rw,
    ):
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            await store_note.func(
                topic="contact:user-alice",
                content="Name: Alice. Likes: Rust.",
            )
            await store_note.func(
                topic="contact:user-alice",
                content="Role: engineer. Likes: Go.",
            )
        identity = await relationship.get_identity(
            "user-alice", other_participant_type="user",
        )
        # name preserved, role added (last-writer-wins scalars), prefs union.
        assert identity == {
            "name": "Alice",
            "prefs": ["Rust", "Go"],
            "role": "engineer",
        }
