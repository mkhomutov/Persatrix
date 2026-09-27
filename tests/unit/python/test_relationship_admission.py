"""v0.3.16 PR B1 — the ``relationship`` tier records what it admits
([ISSUE-0122](../../../docs/issues/ISSUE-0122-relationship-tier-emits-no-provenance.md)).

Every other channel-derived tier pairs its successful
:meth:`MemoryBudget.try_add` with :meth:`MemoryBudget.record_admission`;
the relationship tier charged the budget and stopped there, so under
``PERSATRIX_MEMORY_PROVENANCE=1`` the one tier that answers "does the
persona know who I am?" was invisible, and zero admissions on an
identity turn read the same as a recall miss.  Three layers pin the fix:

* the render helper registers exactly one ``relationship`` admission
  per admitted section, keyed by the pair the row is keyed by
  (``<participant_type>:<participant_id>``) and charged with the tokens
  ``try_add`` actually took;
* a rejected or empty section registers nothing — the registry only
  ever says what reached the prompt;
* the record is ids-only: the provenance log is an egress surface of
  its own, so the identity text (name, role, preferences, raw tail)
  never lands on the structured record.

The full-path test drives an identity turn through
``create_persona_agent`` under the provenance switch and reads the
``persatrix.memory.tier_admitted`` line the operator reads — the line
MT-MEMORY-CROSSROOM-001 Leg 2b said could never exist.
"""

from __future__ import annotations

import enum
import io
import logging

import pytest

from agents.memory.relationship_types import RelationshipSummary
from agents.observability.logging import configure_logging
from agents.persona import create_persona_agent
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_runtime.memory_context import _truncate_with_ellipsis
from agents.persona_runtime.relationship_section import (
    relationship_item_id,
    render_relationship_section,
)
from agents.persona_types import AgentEvent, EventType
from agents.sender_type import sender_type_scope_from_metadata
from agents.session_id import session_scope

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# The identity is made of invented words, so the leak probe can read the
# whole record: no checkout path, user or host name, logger, thread or task
# name, traceback or ids-only field can contain one by accident. Common
# words could: a ``"Rust"`` preference matched a ``rustpin/`` checkout path
# and the tier's own word ``trust``.
_NAME = "Zyxwen"
_ROLE = "glimmerfold steward"
_PREF = "Quillonlang"
_PLACE = "Vexmoor"

_IDENTITY = {
    "name": _NAME,
    "role": _ROLE,
    "prefs": [_PREF],
    "raw": f"Lives in {_PLACE}",
}


# The participant id deliberately shares no text with the identity fields
# above, so the no-leak assertion below cannot pass by accident (an id of
# ``user-zyxwen`` would only fail to contain ``"Zyxwen"`` by letter case).
_ALICE_ID = "p-7f3a"


def _alice(*, interaction_count: int = 0) -> RelationshipSummary:
    return RelationshipSummary(
        other_participant_id=_ALICE_ID,
        other_participant_type="user",
        trust_score=0.5,
        interaction_count=interaction_count,
        last_interaction_at=None,
        notes=None,
        identity=dict(_IDENTITY),
    )


def _render(rel: RelationshipSummary | None, budget: MemoryBudget):
    return render_relationship_section(
        rel,
        budget,
        now=1_000_000.0,
        timezone="UTC",
        truncate=_truncate_with_ellipsis,
    )


def _provenance_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if "tier_admitted" in r.getMessage()
        and getattr(r, "tier", None) == "relationship"
    ]


def _record_text(rec: logging.LogRecord) -> str:
    """Everything on the record that could reach a log sink, lower-cased for
    the leak probe: each attribute's name, and its value both as ``str()``
    and as ``repr()`` (the JSON renderer and the log shipper write a value
    that is not plain JSON as its ``repr()``), plus the formatted message.
    The structured ``extra`` fields land on the record as attributes, so
    they are read the same way."""
    parts = [f"{k} {v} {v!r}" for k, v in vars(rec).items()]
    parts.append(rec.getMessage())
    return " ".join(parts).lower()


_LEAK_PROBES = tuple(word.lower() for word in (_NAME, _ROLE, _PREF, _PLACE))


def _assert_no_leak(text: str) -> None:
    for leak in _LEAK_PROBES:
        assert leak not in text.lower()


class _Who(enum.Enum):
    """An ``extra`` value whose ``str()`` is only ``_Who.NAME``, while its
    ``repr()``, which the JSON renderer writes, carries the name."""

    NAME = _NAME


class TestRecordTextProbe:
    """Each way the record can carry identity text to a sink trips the
    probe: the message (raw or formatted), its args, the exception and stack
    text, and an extra's value, its ``repr()`` or its name."""

    @pytest.mark.parametrize(
        "fields",
        [
            {"msg": f"admitted {_NAME}"},
            {"msg": "admitted %s%s", "args": (_NAME[:3], _NAME[3:])},
            {"msg": "tier_admitted", "args": {"who": _NAME}},
            {"msg": "tier_admitted", "exc_text": f"ValueError: {_NAME}"},
            {"msg": "tier_admitted", "stack_info": f"Stack: {_NAME}"},
            {"msg": "tier_admitted", "item_id": _NAME},
            {"msg": "tier_admitted", "prefs": [_NAME]},
            {"msg": "tier_admitted", "who": _Who.NAME},
            {"msg": "tier_admitted", f"pref_{_NAME}": True},
        ],
        ids=[
            "message", "formatted-args", "unused-args", "exc_text", "stack_info",
            "str-extra", "list-extra", "repr-only-extra", "extra-name",
        ],
    )
    def test_what_the_record_carries_trips_the_probe(
        self, fields: dict[str, object],
    ) -> None:
        assert _NAME.lower() in _record_text(logging.makeLogRecord(fields))


# ─── Registry pairing at the render helper ─────────────────


class TestRelationshipAdmissionRegistry:
    def test_admitted_section_registers_one_relationship_admission(self) -> None:
        budget = MemoryBudget(total_tokens=1500)
        section = _render(_alice(), budget)
        assert section is not None
        assert budget.admissions_by_tier("relationship") == [f"user:{_ALICE_ID}"]

    def test_item_id_is_the_row_key_not_the_identity_text(self) -> None:
        """The pair the row is keyed by, in ``<type>:<id>`` form — so the
        participant type the read matched (an admitted but mistyped row is
        the ISSUE-0119 class, MT-MEMORY-CROSSROOM-001 Leg 2b diagnosis
        mode 1) is on the record."""
        assert relationship_item_id(_alice()) == f"user:{_ALICE_ID}"
        peer = RelationshipSummary(
            other_participant_id="iron-fox",
            other_participant_type="agent",
            trust_score=0.5,
            interaction_count=2,
            last_interaction_at=None,
            notes=None,
        )
        assert relationship_item_id(peer) == "agent:iron-fox"

    def test_rejected_section_registers_nothing(self) -> None:
        """A budget too small to admit the section leaves the registry
        empty — the registry says what reached the prompt, nothing else."""
        budget = MemoryBudget(total_tokens=1)
        assert _render(_alice(), budget) is None
        assert budget.admissions_by_tier("relationship") == []

    def test_empty_relationship_registers_nothing(self) -> None:
        budget = MemoryBudget(total_tokens=1500)
        assert _render(None, budget) is None
        empty = RelationshipSummary(
            other_participant_id="iron-fox",
            other_participant_type="agent",
            trust_score=0.5,
            interaction_count=0,
            last_interaction_at=None,
            notes=None,
        )
        assert _render(empty, budget) is None
        assert budget.admissions_by_tier("relationship") == []


# ─── The structured record under the provenance switch ─────


class TestRelationshipProvenanceEmission:
    def test_emits_one_record_with_the_charged_tokens(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_MEMORY_PROVENANCE", "1")
        budget = MemoryBudget(total_tokens=1500)
        before = budget.remaining
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime"):
            section = _render(_alice(interaction_count=3), budget)
        assert section is not None
        charged = before - budget.remaining
        assert charged > 0
        records = _provenance_records(caplog)
        assert len(records) == 1
        rec = records[0]
        assert getattr(rec, "item_id", None) == f"user:{_ALICE_ID}"
        assert getattr(rec, "tokens_admitted", None) == charged

    def test_record_never_carries_the_identity_text(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The provenance log is its own egress surface: ids, tier and
        tokens travel; the name / role / prefs / raw tail do not."""
        monkeypatch.setenv("PERSATRIX_MEMORY_PROVENANCE", "1")
        budget = MemoryBudget(total_tokens=1500)
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime"):
            section = _render(_alice(), budget)
        assert section is not None
        # The section itself carries the identity — that is its job.
        assert _NAME in section.content
        (rec,) = _provenance_records(caplog)
        _assert_no_leak(_record_text(rec))

    def test_rendered_line_never_carries_the_identity_text(
        self,
        rendered_log: io.StringIO,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same probe on the JSON line an operator reads: what actually
        leaves the process, not the record that stands in for it."""
        monkeypatch.setenv("PERSATRIX_MEMORY_PROVENANCE", "1")
        configure_logging(service_kind="agent", service_instance="ember-owl")
        assert _render(_alice(), MemoryBudget(total_tokens=1500)) is not None
        rendered = rendered_log.getvalue()
        assert "persatrix.memory.tier_admitted" in rendered
        _assert_no_leak(rendered)

    def test_silent_without_the_switch(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default behaviour is unchanged: the registry fills, the log
        does not."""
        monkeypatch.delenv("PERSATRIX_MEMORY_PROVENANCE", raising=False)
        budget = MemoryBudget(total_tokens=1500)
        with caplog.at_level(logging.DEBUG, logger="agents.persona_runtime"):
            assert _render(_alice(), budget) is not None
        assert budget.admissions_by_tier("relationship") == [f"user:{_ALICE_ID}"]
        assert _provenance_records(caplog) == []


# ─── Full path: an identity turn is now visible to the operator ──


class TestIdentityTurnIsVisibleToProvenance:
    async def test_identity_turn_emits_one_relationship_admission(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The MT-MEMORY-CROSSROOM-001 Leg 2b shape: identity stated in one
        room, the persona addressed in another with an empty transcript.
        Under the switch the turn now emits exactly one ``relationship``
        admission keyed ``user:<sender>`` — so zero admissions on such a
        turn is a recall (or wiring) miss, no longer the expected reading."""
        monkeypatch.setenv("PERSATRIX_MEMORY_PROVENANCE", "1")
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        try:
            store_note = next(
                td for td in agent._memory_tools if td.name == "store_note"
            )
            assert store_note.func is not None
            with session_scope("room-a"), sender_type_scope_from_metadata(
                {"sender_participant_type": "user"},
            ):
                res = await store_note.func(
                    topic=f"contact:{_ALICE_ID}",
                    content=(
                        f"Name: {_NAME}. Role: {_ROLE}. "
                        f"Prefers: {_PREF}. Lives in {_PLACE}."
                    ),
                )
            assert res.success

            event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "Morning — anything here that needs me?"},
                sender_id=_ALICE_ID,
                metadata={"sender_participant_type": "user"},
            )
            with (
                caplog.at_level(logging.INFO, logger="agents.persona_runtime"),
                session_scope("room-b"),
            ):
                await agent._inject_memory_context(event)

            rel_section = agent._working_memory.get_section("relationship_context")
            assert rel_section is not None
            # All four identity fields reach the prompt on this path, so the
            # probe below has something to catch.
            for value in (_NAME, _ROLE, _PREF, _PLACE):
                assert value in rel_section.content
            records = _provenance_records(caplog)
            assert len(records) == 1
            assert getattr(records[0], "item_id", None) == f"user:{_ALICE_ID}"
            assert getattr(records[0], "tokens_admitted", 0) > 0
            # Same probe as the render-level test: the structured fields
            # are attributes, so the message alone proves nothing.
            _assert_no_leak(_record_text(records[0]))
        finally:
            await agent.close_memory()
