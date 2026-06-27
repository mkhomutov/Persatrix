"""RFC 0051 Phase 1b (v0.3.10) — the ``agent.deliberated`` audit, **dark**.

PR 2 of the RFC 0051 PR plan (``docs/rfcs/0051-pr-plan.md``). PR 1 restructured
the *bid* into a structured ``{ should_post, reason_code, reason_note }`` verdict
behind an internal ``mode``; this file pins the *seam* wiring that PR 2 adds:
:func:`agents.persona_runtime.salience_gate.run_salience_gate` threading ``mode``
through to the bid and emitting the ``agent.deliberated`` audit record on the
deliberation path.

Load-bearing contracts:

* **Audit only under reasoning.** Under ``mode: off`` (prod default until PR 6)
  the seam is byte-for-byte the v0.3.8 scalar gate — it emits **no**
  ``agent.deliberated`` record. The audit fires only on the structured
  (``bid``/``plan``) rungs, so deploying Phase 1 stays inert in production.
* **Decision + reason_code + counts, never the prose.** The record carries the
  closed-set ``reason_code`` (assertable, unlike free text), the ``should_post``
  decision, and low-cardinality counts — and **never** the verbatim
  ``reason_note`` or any CompositionPlan (RFC 0051 §E privacy wall). The wall is
  structural here: the seam never even reads ``reason_note`` onto the payload.
* **Both verdicts deliberate.** A ``should_post=false`` (silence) turn and a
  ``should_post=true`` (speak) turn each emit exactly one record — a
  deliberation happened either way.

The bid (``evaluate_salience``) is patched in the ``salience_gate`` namespace so
these assertions are about the *seam*, independent of the bid's prompt / model
resolution (that is ``test_salience_bid_reasoning.py``'s job).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from agents.observability import logging as logging_mod
from agents.observability.logging import configure_logging
from agents.observability.redact import NoopRedactor
from agents.persona_runtime.salience_gate import SalienceOutcome, run_salience_gate
from agents.persona_types import AgentEvent, EventType
from agents.response_gate import POLICY_ALWAYS, GateDecision
from agents.salience_bid import SalienceDecision
from agents.salience_deliberation import (
    MODE_BID,
    MODE_OFF,
    MODE_PLAN,
    REASON_ADDS_SUBSTANCE,
    REASON_ALREADY_ANSWERED,
)

pytestmark = pytest.mark.asyncio

_AUDIT_EVENT = "agent.deliberated"
# A distinctive free-text justification — if it ever appears in the audit
# record the privacy wall (RFC 0051 §E) has been breached.
_SECRET_NOTE = "iron-fox already answered the database question two turns ago"

# Patch the bid where the seam looks it up, not where it is defined.
_BID_PATH = "agents.persona_runtime.salience_gate.evaluate_salience"


def _open_floor_decision() -> GateDecision:
    """The Tier-A verdict that admits an ambiguous open-floor participant —
    the only decision :func:`is_open_floor_admit` matches (TB1)."""
    return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")


def _event(*, governed: bool = True, channel_size: int | None = None) -> AgentEvent:
    payload: dict[str, Any] = {
        "content": "What database should we pick for the cache?",
        "respond_policy": "always",
    }
    if governed:
        payload["salience_gated"] = True
    if channel_size is not None:
        payload["channel_size"] = channel_size
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id="group:planning",
        sender_id="alice",
    )


def _stub_agent(*, seed: list[dict[str, Any]] | None = None) -> MagicMock:
    """A minimal stand-in for ``_LLMPersonaAgent`` exposing exactly the
    attributes / coroutines :func:`run_salience_gate` calls. The bid itself is
    patched, so ``_llm_client`` / ``name`` / ``role`` only need to exist."""
    if seed is None:
        seed = [
            {"role": "user", "content": "We should pick a cache database."},
            {"role": "assistant", "content": "Redis is the obvious fit."},
            {"role": "user", "content": "What database should we pick for the cache?"},
        ]
    agent = MagicMock()
    agent.agent_id = "ember-owl"
    agent.name = "Ember Owl"
    agent.role = "Planner"
    agent._llm_client = MagicMock()
    agent._format_event = MagicMock(return_value="formatted message")
    agent._build_seed_messages = AsyncMock(return_value=seed)
    agent._store_event_episode = AsyncMock(return_value=None)
    return agent


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every captured ``audit=True`` record naming the deliberation event."""
    return [
        rec
        for rec in caplog.records
        if getattr(rec, "audit", None) is True
        and rec.getMessage() == _AUDIT_EVENT
    ]


async def _patched_bid(monkeypatch: pytest.MonkeyPatch, decision: SalienceDecision) -> AsyncMock:
    bid = AsyncMock(return_value=decision)
    monkeypatch.setattr(_BID_PATH, bid)
    return bid


# ─── Audit fires on the structured (reasoning) path ──────────────────────────


class TestDeliberatedAuditEmitted:
    async def test_silence_verdict_emits_one_record_with_reason_code(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``should_post=false`` turn → silence outcome + exactly one
        ``agent.deliberated`` record carrying the closed-set ``reason_code``."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(
                speak=False, score=None, reason=REASON_ALREADY_ANSWERED,
                reason_note=_SECRET_NOTE,
            ),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            outcome = await run_salience_gate(
                agent, _event(), _open_floor_decision(), mode=MODE_BID,
            )

        assert outcome == SalienceOutcome(silence=True)
        records = _audit_records(caplog)
        assert len(records) == 1
        rec = records[0]
        assert getattr(rec, "should_post") is False
        assert getattr(rec, "reason_code") == REASON_ALREADY_ANSWERED
        assert getattr(rec, "agent_id") == "ember-owl"

    async def test_speak_verdict_emits_one_record_with_should_post_true(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``should_post=true`` turn → speak outcome + one ``agent.deliberated``
        record (a deliberation happened on the speak path too)."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(
                speak=True, score=None, reason=REASON_ADDS_SUBSTANCE,
                reason_note=_SECRET_NOTE,
            ),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            outcome = await run_salience_gate(
                agent, _event(), _open_floor_decision(), mode=MODE_PLAN,
            )

        assert outcome is not None and outcome.silence is False
        records = _audit_records(caplog)
        assert len(records) == 1
        assert getattr(records[0], "should_post") is True
        assert getattr(records[0], "reason_code") == REASON_ADDS_SUBSTANCE

    async def test_record_carries_transcript_count_not_content(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The counts are low-cardinality integers — the transcript *size*, not
        its content. Seed has 3 turns; the current message is the last, so the
        prior transcript the bid saw is 2 turns."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(speak=False, score=None, reason=REASON_ALREADY_ANSWERED),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            await run_salience_gate(agent, _event(), _open_floor_decision(), mode=MODE_BID)

        rec = _audit_records(caplog)[0]
        assert getattr(rec, "transcript_turns") == 2


# ─── The privacy wall (RFC 0051 §E) ──────────────────────────────────────────


class TestDeliberatedAuditNeverLeaksProse:
    async def test_reason_note_is_absent_from_the_record(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The verbatim ``reason_note`` must never reach the audit egress — not
        as a field, not embedded in any other field (RFC 0051 §E / §Security)."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(
                speak=False, score=None, reason=REASON_ALREADY_ANSWERED,
                reason_note=_SECRET_NOTE,
            ),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            await run_salience_gate(agent, _event(), _open_floor_decision(), mode=MODE_BID)

        rec = _audit_records(caplog)[0]
        assert not hasattr(rec, "reason_note")
        assert not hasattr(rec, "plan")
        # Defence in depth: the secret clause appears in no string attribute.
        blob = " ".join(str(v) for v in rec.__dict__.values())
        assert _SECRET_NOTE not in blob


# ─── Dark: no audit on the scalar (mode: off) path ───────────────────────────


class TestScalarPathStaysDark:
    async def test_mode_off_emits_no_deliberated_audit(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Under ``mode: off`` the seam runs the scalar score gate and emits no
        ``agent.deliberated`` record — Phase 1 stays inert in production."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(speak=False, score=0.1, reason="below_threshold"),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            outcome = await run_salience_gate(
                agent, _event(), _open_floor_decision(), mode=MODE_OFF,
            )

        assert outcome == SalienceOutcome(silence=True)
        assert _audit_records(caplog) == []

    async def test_default_mode_is_off_and_dark(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``mode`` parameter defaults to ``off`` — the action-loop call site
        passes nothing, so production stays on the scalar path (no audit)."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(speak=True, score=0.9, reason="salient"),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            await run_salience_gate(agent, _event(), _open_floor_decision())

        assert _audit_records(caplog) == []


# ─── No bid, no deliberation: skip paths emit nothing ────────────────────────


class TestNoDeliberationNoAudit:
    async def test_ungoverned_channel_emits_no_audit(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ungoverned channel never reaches the bid (``run_salience_gate``
        returns ``None``) — so even under ``mode: plan`` there is no audit."""
        bid = await _patched_bid(
            monkeypatch,
            SalienceDecision(speak=False, score=None, reason=REASON_ALREADY_ANSWERED),
        )
        agent = _stub_agent()
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            outcome = await run_salience_gate(
                agent, _event(governed=False), _open_floor_decision(), mode=MODE_PLAN,
            )

        assert outcome is None
        bid.assert_not_awaited()
        assert _audit_records(caplog) == []

    async def test_oversized_channel_skip_emits_no_audit(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TB6: above the channel-size cap the bid is *skipped* — no deliberation
        occurred, so no ``agent.deliberated`` record is emitted even under
        ``mode: plan`` (the skip is a structural fall-back, not a verdict)."""
        bid = await _patched_bid(
            monkeypatch,
            SalienceDecision(speak=True, score=None, reason=REASON_ADDS_SUBSTANCE),
        )
        agent = _stub_agent()
        event = _event(channel_size=999)
        event.payload["salience_max_channel_members"] = 20
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime.salience_gate"):
            outcome = await run_salience_gate(
                agent, event, _open_floor_decision(), mode=MODE_PLAN,
            )

        assert outcome == SalienceOutcome(silence=True)
        bid.assert_not_awaited()
        assert _audit_records(caplog) == []


# ─── ISSUE-0108: the audit payload reaches the *rendered* egress ─────────────
#
# Every test above asserts on the stdlib ``LogRecord`` (via ``caplog``) — the
# layer the bug was invisible at, because the record always carried the
# ``extra=`` keys as attributes. ISSUE-0108 was that those keys were dropped
# from the *rendered / shipped* JSON line: ``foreign_pre_chain`` had no
# ``ExtraAdder``, so the operator saw a presence-only ``agent.deliberated``
# event (message + trace context) with no ``reason_code`` / ``should_post``.
# These tests drive the seam through ``configure_logging``'s real renderer and
# assert on the emitted JSON — the surface an operator actually reads.


@pytest.fixture
def _rendered_log(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    """Force ``configure_logging`` to rebuild its chain and render to a buffer.

    Mirrors ``agents/tests/test_observability_logging.py``'s capture: reset the
    module-global ``_configured`` flag + structlog defaults so the chain is
    rebuilt, and swap ``sys.stderr`` (and ``logging_mod.sys``, since the
    ``StreamHandler`` binds the module's ``sys`` reference at build time) for a
    ``StringIO`` the handler writes into."""
    import sys as _real_sys

    structlog.contextvars.clear_contextvars()
    logging_mod._configured = False
    logging_mod._redactor = NoopRedactor()
    structlog.reset_defaults()

    buf = io.StringIO()

    class _SysShim:
        stderr = buf

        def __getattr__(self, name: str) -> Any:  # pragma: no cover - trivial
            return getattr(_real_sys, name)

    monkeypatch.setattr("sys.stderr", buf)
    monkeypatch.setattr(logging_mod, "sys", _SysShim())
    yield buf

    structlog.contextvars.clear_contextvars()
    logging_mod._configured = False
    logging_mod._redactor = NoopRedactor()
    structlog.reset_defaults()


def _rendered_audit_line(buf: io.StringIO) -> dict[str, Any]:
    """The single rendered ``agent.deliberated`` JSON line in the buffer."""
    lines = [
        json.loads(line)
        for line in buf.getvalue().strip().splitlines()
        if line.startswith("{")
    ]
    audit = [rec for rec in lines if rec.get("message") == _AUDIT_EVENT]
    assert len(audit) == 1, f"expected one rendered audit line, got {audit}"
    return audit[0]


class TestDeliberatedAuditRenderedEgress:
    async def test_reason_code_and_decision_reach_the_rendered_line(
        self, _rendered_log: io.StringIO, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The payload the audit was *designed* to carry now renders: the
        operator can read the deliberation ``reason_code`` / ``should_post`` /
        ``transcript_turns`` off the JSON line (ISSUE-0108 Gap A)."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(
                speak=False, score=None, reason=REASON_ALREADY_ANSWERED,
                reason_note=_SECRET_NOTE,
            ),
        )
        configure_logging(service_kind="agent", service_instance="ember-owl")
        await run_salience_gate(
            _stub_agent(), _event(), _open_floor_decision(), mode=MODE_BID,
        )

        rec = _rendered_audit_line(_rendered_log)
        assert rec["audit"] is True
        assert rec["should_post"] is False
        assert rec["reason_code"] == REASON_ALREADY_ANSWERED
        assert rec["transcript_turns"] == 2
        assert rec["agent_id"] == "ember-owl"

    async def test_reason_note_never_reaches_the_rendered_line(
        self, _rendered_log: io.StringIO, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Surfacing the audit payload must not surface the verbatim
        ``reason_note`` — the §E wall is structural (the seam never reads it onto
        the record), so it appears in no field of the rendered JSON either."""
        await _patched_bid(
            monkeypatch,
            SalienceDecision(
                speak=False, score=None, reason=REASON_ALREADY_ANSWERED,
                reason_note=_SECRET_NOTE,
            ),
        )
        configure_logging(service_kind="agent", service_instance="ember-owl")
        await run_salience_gate(
            _stub_agent(), _event(), _open_floor_decision(), mode=MODE_BID,
        )

        rec = _rendered_audit_line(_rendered_log)
        assert "reason_note" not in rec
        assert _SECRET_NOTE not in json.dumps(rec)
