"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D3** — the
``store_note(contact:<id>)`` → ``upsert_identity`` write-through, with the
D2 dual-write retired: a contact note now lands on the cross-room
relationship tier **only** — no room-scoped note row is written.

D2 dual-wrote (relationship identity *and* the legacy room-scoped note) so
cross-room recall could be migrated and verified before the note write was
dropped.  D3 drops it: when a relationship handle is present and the
structured parse yields identity, the contact note routes to the
relationship tier alone.  The note write survives only as a **fallback
safety net** — when there is no relationship handle, or the identity
upsert raises — so nothing the model stored is ever silently lost.  This
file covers:

* the pure :func:`agents.memory.identity_parse.parse_identity_fields`
  structuring step (keyed / natural / unkeyed-to-``raw``);
* the :data:`agents.sender_type` task-local participant-type binding;
* the write-through at the ``store_note`` tool boundary — identity lands
  on the relationship row with the bound participant type, **no note row
  is written** for the contact topic, non-contact topics are untouched,
  and the fallback note write fires when there is no relationship handle
  or the identity write fails;
* merge/supersede across two notes via the parser + ``merge_identity``.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.identity_parse import parse_identity_fields
from agents.memory.relationship import RelationshipMemory
from agents.sender_type import (
    current_sender_type,
    normalize_sender_type,
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

    def test_natural_name_phrase_rejects_prose(self):
        """The narrow natural-name branch only fires for a proper-noun-shaped
        capture (capitalized, short), so conversational prose is not
        mis-stored as the contact's *name*.

        Rationale (PR #554 deep-review #2): an unkeyed clause like "I am
        happy to help" matched ``i am (.+)`` and became ``name="happy to
        help"`` — which renders as the load-bearing "who is this" line and,
        because ``name`` is scalar last-writer-wins, can clobber a real
        name. The prose is still preserved verbatim under ``raw`` (nothing
        lost), just not promoted to ``name``."""
        assert parse_identity_fields("I am happy to help") == {
            "raw": "I am happy to help",
        }
        assert parse_identity_fields("call me later when you can") == {
            "raw": "call me later when you can",
        }

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


class TestNormalizeSenderType:
    """The shared resolver both the write (scope-binding) and read (recall)
    sides funnel a raw ``sender_participant_type`` value through, so the
    identity write for an event and the identity read back for it always
    resolve to the *same* relationship row (PR #554 deep-review #3 — the two
    sides previously normalized differently; a whitespace-padded type would
    write under ``"user"`` but read under ``" user "`` and silently miss)."""

    def test_strips_surrounding_whitespace(self):
        assert normalize_sender_type("  user  ") == "user"

    def test_blank_or_non_string_falls_back_to_agent(self):
        assert normalize_sender_type("") == "agent"
        assert normalize_sender_type("   ") == "agent"
        assert normalize_sender_type(None) == "agent"
        assert normalize_sender_type(123) == "agent"


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

    async def test_contact_note_not_written_to_notes_tier(
        self, episodic, relationship, gate_rw,
    ):
        """D3: the legacy room-scoped note write is retired — a contact note
        with a relationship handle lands on the relationship tier *only*, so
        no row appears in the notes tier (the seam cannot recur because
        identity no longer lives in a room-scoped tier)."""
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            result = await store_note.func(
                topic="contact:user-alice", content="Name: Alice.",
            )
        assert result.success
        # No note row was written (recall on any query is empty).
        assert await episodic.recall_notes("Alice", limit=10) == []
        assert await episodic.recall_notes("", limit=10) == []
        # The identity still landed on the relationship tier.
        assert await relationship.get_identity(
            "user-alice", other_participant_type="user",
        ) == {"name": "Alice"}

    async def test_contact_note_with_tags_still_writes_identity_only(
        self, episodic, relationship, gate_rw,
    ):
        """Tags on a contact note are *not* a reason to fall back to a note
        write: the identity tier has no tag field, so a tagged ``contact:*``
        note still lands on the relationship tier alone and writes no note
        row (the tags are discarded). Pins the D3 invariant — nothing, not
        even a tag, may reintroduce the room-scoped note the cross-room seam
        lived in."""
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            result = await store_note.func(
                topic="contact:user-alice", content="Name: Alice.",
                tags="vip,engineering",
            )
        assert result.success
        # The tags did not cause a fallback note write.
        assert await episodic.recall_notes("Alice", limit=10) == []
        assert await episodic.recall_notes("", limit=10) == []
        # Identity still landed on the relationship tier (tags discarded).
        assert await relationship.get_identity(
            "user-alice", other_participant_type="user",
        ) == {"name": "Alice"}

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

    async def test_no_relationship_handle_falls_back_to_note(
        self, episodic, gate_rw,
    ):
        """No relationship handle (pre-wiring / non-persona callers): the
        identity tier is unreachable, so the contact note falls back to a
        room-scoped note write — the tool succeeds and nothing is lost."""
        tools = create_memory_tools(episodic, gate_rw)
        store_note = _store_note(tools)
        result = await store_note.func(
            topic="contact:user-alice", content="Name: Alice.",
        )
        assert result.success
        # Fallback safety net: the note was written since identity could not be.
        notes = await episodic.recall_notes("Alice", limit=10)
        assert any("Alice" in n.content for n in notes)

    async def test_identity_write_failure_falls_back_to_note(
        self, episodic, relationship, gate_rw, monkeypatch,
    ):
        """If the identity upsert raises, the contact note falls back to the
        room-scoped note write so the model's data is never silently dropped —
        the tool still succeeds."""
        async def _boom(*args, **kwargs):
            raise RuntimeError("identity backend down")

        monkeypatch.setattr(relationship, "upsert_identity", _boom)
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        result = await store_note.func(
            topic="contact:user-alice", content="Name: Alice.",
        )
        assert result.success
        # Fallback: the note was written because the identity write failed.
        notes = await episodic.recall_notes("Alice", limit=10)
        assert any("Alice" in n.content for n in notes)

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

    async def test_unkeyed_raw_detail_survives_second_note(
        self, episodic, relationship, gate_rw,
    ):
        """An unkeyed fact captured in note 1 ("Lives in Berlin") is still on
        the identity row after note 2 adds a different unkeyed fact — the
        cross-note ``raw`` union (PR #554 deep-review #1) end-to-end through
        the write-through, not just the pure merge."""
        tools = create_memory_tools(episodic, gate_rw, relationship=relationship)
        store_note = _store_note(tools)
        with sender_type_scope_from_metadata({"sender_participant_type": "user"}):
            await store_note.func(
                topic="contact:user-alice",
                content="Name: Alice. Lives in Berlin",
            )
            await store_note.func(
                topic="contact:user-alice",
                content="Role: engineer. Speaks German",
            )
        identity = await relationship.get_identity(
            "user-alice", other_participant_type="user",
        )
        assert identity == {
            "name": "Alice",
            "role": "engineer",
            "raw": "Lives in Berlin. Speaks German",
        }
