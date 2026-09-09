"""ISSUE-0132 (v0.3.16 PR A2) — the audience shadow trace.

The verdict scope lock 1 gates the flip on is rendered from these
records, so this module pins the trace's *shape* (the measurement's
input contract) and its *egress bound* (the process log is its own
surface — the facts-shadow rule: ids, levels and room ids ride, content
never does).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

import pytest

from agents.persona_runtime.audience import (
    AUDIENCE_LIVE,
    AUDIENCE_OFF,
    AUDIENCE_SHADOW,
    AudienceVerdict,
    TurnAudience,
)
from agents.persona_runtime.audience_shadow import (
    SHADOW_LOGGER_NAME,
    SHADOW_TRACE_ATTR,
    emit_audience_shadow,
)
from agents.persona_runtime.injection_gate import TurnInjectionGate

DM = "dm:alice:iron-fox"
ROOM = "group:standup"


@dataclass(frozen=True)
class _Entry:
    id: str
    protection_level: str | None
    source_channel_id: str | None
    #: The natural-language payload that must never reach the log.
    object: str = "the Zephyr acquisition closes March 3"


def _audience(mode: str = AUDIENCE_SHADOW, *, fetches: int = 1) -> TurnAudience:
    return TurnAudience(
        mode=mode, acting_channel_id=ROOM,
        rooms={
            ROOM: frozenset({"alice", "iron-fox", "bob"}),
            DM: frozenset({"alice", "iron-fox"}),
        },
        fetches=fetches, source_rooms=1,
    )


def _run(mode: str, entries: list[_Entry]) -> TurnInjectionGate:
    gate = TurnInjectionGate(
        acting="internal", agent_id="iron-fox",
        audience=None if mode == AUDIENCE_OFF else _audience(mode),
    )
    gate.filter_entries("facts", entries)
    return gate


def _trace(caplog: pytest.LogCaptureFixture) -> dict | None:
    for record in caplog.records:
        payload = getattr(record, SHADOW_TRACE_ATTR, None)
        if isinstance(payload, dict):
            return payload
    return None


def test_trace_carries_the_four_verdict_counts_and_the_fetch_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = _run(AUDIENCE_SHADOW, [
        _Entry("e1", "internal", DM),        # disjoint (Bob is in the room)
        _Entry("e2", "internal", ROOM),      # admit (same room)
        _Entry("e3", "internal", None),      # no provenance
        _Entry("e4", "internal", "group:x"), # fetch failed (never resolved)
    ])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW,
            audience=_audience(),
        )

    trace = _trace(caplog)
    assert trace is not None
    assert trace["tier"] == "audience"
    assert trace["mode"] == AUDIENCE_SHADOW
    assert trace["acting"] == "internal"
    assert trace["acting_channel_id"] == ROOM
    assert trace["verdicts"] == {
        AudienceVerdict.ADMIT.value: 1,
        AudienceVerdict.WITHHOLD_DISJOINT.value: 1,
        AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED.value: 1,
        AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE.value: 1,
    }
    assert trace["fetches"] == 1
    assert trace["source_rooms"] == 1
    # In shadow nothing was actually withheld — that IS the posture.
    assert trace["withheld"] == 0
    assert trace["judged"] == 4
    # Per-tier counts ride at INFO too: the measurement has to be able to
    # say WHICH of the judged tiers a sample exercised, and a delta over
    # one tier is a sample, not a measurement.
    assert trace["by_tier"] == {"facts": {
        AudienceVerdict.WITHHOLD_DISJOINT.value: 1,
        AudienceVerdict.ADMIT.value: 1,
        AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE.value: 1,
        AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED.value: 1,
    }}
    # The per-entry array is DEBUG-only — see the volume rule below.
    assert "candidates" not in trace


def test_live_reports_what_it_actually_withheld(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = _run(AUDIENCE_LIVE, [_Entry("e1", "internal", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_LIVE,
            audience=_audience(AUDIENCE_LIVE),
        )
    trace = _trace(caplog)
    assert trace is not None
    assert trace["withheld"] == 1
    assert trace["mode"] == AUDIENCE_LIVE


def test_the_trace_never_carries_entry_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The facts-shadow egress bound: dumping protected content into the
    process log would undo at the log what the gate enforces at the
    prompt."""
    gate = _run(AUDIENCE_SHADOW, [_Entry("e1", "internal", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW,
            audience=_audience(),
        )
    trace = _trace(caplog)
    assert trace is not None
    assert "March 3" not in repr(trace)
    assert "March 3" not in caplog.text


def test_the_per_entry_array_is_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Volume: every number the verdict is rendered from is constant-size
    and rides at INFO; the per-entry array — up to the judged tiers'
    recall limits, and read by no consumer of the measurement — is
    attached only when this logger is enabled for DEBUG.  The eval
    harness lowers it there, so the seeds keep their evidence."""
    gate = _run(AUDIENCE_SHADOW, [_Entry("e1", "internal", DM)])
    with caplog.at_level(logging.DEBUG, logger=SHADOW_LOGGER_NAME):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW,
            audience=_audience(),
        )
    trace = _trace(caplog)
    assert trace is not None
    assert trace["candidates"] == [{
        "tier": "facts", "entry_id": "e1", "protection_level": "internal",
        "source_channel_id": DM, "verdict": "withhold-disjoint",
    }]
    assert "March 3" not in repr(trace)


@pytest.mark.parametrize("mode", [AUDIENCE_OFF, AUDIENCE_SHADOW])
def test_a_turn_with_no_verdicts_emits_nothing(
    caplog: pytest.LogCaptureFixture, mode: str,
) -> None:
    """A turn whose candidates were all ``public`` measured nothing and
    spent nothing — the facts-shadow quiet-turn rule."""
    gate = _run(mode, [_Entry("e1", "public", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=mode,
            audience=None if mode == AUDIENCE_OFF else _audience(fetches=0),
        )
    assert _trace(caplog) is None


def test_a_turn_that_paid_for_a_fetch_always_traces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one quiet turn that must not be quiet: a round trip this turn
    spent is a cost the scope-lock-2 bound has to see, and suppressing the
    record would make the only shape worth catching — fetches with
    nothing judged — the one shape invisible to the measurement."""
    gate = _run(AUDIENCE_SHADOW, [_Entry("e1", "public", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW,
            audience=_audience(),
        )
    trace = _trace(caplog)
    assert trace is not None
    assert (trace["judged"], trace["fetches"]) == (0, 1)


def test_a_view_inconsistent_with_the_gate_still_traces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``audience=None`` beside a gate holding records is a supported
    input, not an error: the counts still render, the room fields degrade
    to ``None``/0."""
    gate = _run(AUDIENCE_SHADOW, [_Entry("e1", "internal", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW, audience=None,
        )
    trace = _trace(caplog)
    assert trace is not None
    assert trace["judged"] == 1
    assert trace["acting_channel_id"] is None
    assert (trace["fetches"], trace["source_rooms"]) == (0, 0)


def test_emission_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """``_inject_memory_context``'s never-fail contract: observability
    must not be able to cost a turn.  Exercised through a genuinely
    poisoned decision record — a verdict object with no ``.value`` —
    because ``audience=None`` is a supported input and would leave the
    ``except`` arm this contract lives in unexercised."""
    gate = _run(AUDIENCE_SHADOW, [_Entry("e1", "internal", DM)])
    poisoned = SimpleNamespace(
        tier="facts", entry_id="e1", protection_level="internal",
        source_channel_id=DM, verdict=object(),
    )
    with (
        mock.patch.object(
            type(gate), "audience_records",
            new_callable=mock.PropertyMock, return_value=(poisoned,),
        ),
        caplog.at_level(logging.INFO),
    ):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW,
            audience=_audience(),
        )

    # The turn survives; the failure is reported, not raised, and no
    # half-built trace rides out under the measurement's attribute.
    assert _trace(caplog) is None
    assert "audience shadow trace failed" in caplog.text
