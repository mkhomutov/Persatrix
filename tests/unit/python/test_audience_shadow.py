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

import pytest

from agents.persona_runtime.audience import (
    AUDIENCE_LIVE,
    AUDIENCE_OFF,
    AUDIENCE_SHADOW,
    AudienceVerdict,
    TurnAudience,
)
from agents.persona_runtime.audience_shadow import (
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


def _audience(mode: str = AUDIENCE_SHADOW) -> TurnAudience:
    return TurnAudience(
        mode=mode, acting_channel_id=ROOM,
        rooms={
            ROOM: frozenset({"alice", "iron-fox", "bob"}),
            DM: frozenset({"alice", "iron-fox"}),
        },
        fetches=1, source_rooms=1,
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
    assert trace["unknown_label"] == 0
    assert len(trace["candidates"]) == 4


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
    assert trace["candidates"][0] == {
        "tier": "facts", "entry_id": "e1", "protection_level": "internal",
        "source_channel_id": DM, "verdict": "withhold-disjoint",
    }


@pytest.mark.parametrize("mode", [AUDIENCE_OFF, AUDIENCE_SHADOW])
def test_a_turn_with_no_verdicts_emits_nothing(
    caplog: pytest.LogCaptureFixture, mode: str,
) -> None:
    """Single-room deployments see zero log volume — the facts-shadow
    quiet-turn rule."""
    gate = _run(mode, [_Entry("e1", "public", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=mode,
            audience=None if mode == AUDIENCE_OFF else _audience(),
        )
    assert _trace(caplog) is None


def test_emission_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """``_inject_memory_context``'s never-fail contract: observability
    must not be able to cost a turn."""
    gate = _run(AUDIENCE_SHADOW, [_Entry("e1", "internal", DM)])
    with caplog.at_level(logging.INFO):
        emit_audience_shadow(
            gate, agent_id="iron-fox", mode=AUDIENCE_SHADOW,
            audience=None,  # inconsistent with the gate — must degrade
        )
    # No exception; the turn survives whatever the trace could not say.
