"""ISSUE-0108 Gap B (v0.3.16 PR B2) — the verbatim ``reason_note`` gets its one egress.

RFC 0051 §E names the operator-debug **agent log** as the only place the
verbatim silence ``reason_note`` may appear. From v0.3.10 to v0.3.15 nothing
wrote it anywhere (the operator-reveal PR 7 was cut), so the reason a persona
went silent was readable only as the ``deliberation.suppressed{reason_code}``
metric label. This file pins the egress
:func:`agents.persona_runtime.salience_gate.run_salience_gate` now emits:

* **One DEBUG record, on the suppression path, on the structured rungs.** A
  ``should_post=false`` verdict under ``bid``/``plan`` that carries a note emits
  exactly one ``agent.deliberation.reason_note`` record with the verbatim note,
  the closed-set ``reason_code`` and the agent/channel ids.
* **Nowhere else.** A speak verdict, a verdict with no note, and the scalar
  ``off`` rung emit nothing; the ``agent.deliberated`` audit stays note-free.
* **Not an audit record.** The line carries no ``audit=True``, so the RFC 0009
  audit registry never sees it — the §E "two egress paths" contract.
* **Silent at the INFO default.** Through ``configure_logging``'s real renderer
  the note appears on the rendered JSON line only when the agent runs at
  ``DEBUG``; at the default level the audit line renders and the note does not.

The bid is patched in the ``salience_gate`` namespace, as in
``test_salience_gate_deliberation_audit.py``, so the assertions are about the
seam alone.
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
_NOTE_EVENT = "agent.deliberation.reason_note"
_LOGGER = "agents.persona_runtime.salience_gate"
# A distinctive clause: it must appear on the DEBUG record verbatim and on
# nothing else.
_NOTE = "iron-fox already answered the database question two turns ago"

_BID_PATH = "agents.persona_runtime.salience_gate.evaluate_salience"


def _open_floor_decision() -> GateDecision:
    return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")


def _event() -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "What database should we pick for the cache?",
            "respond_policy": "always",
            "salience_gated": True,
        },
        channel_id="group:planning",
        sender_id="alice",
    )


def _stub_agent() -> MagicMock:
    agent = MagicMock()
    agent.agent_id = "ember-owl"
    agent.name = "Ember Owl"
    agent.role = "Planner"
    agent._llm_client = MagicMock()
    agent._format_event = MagicMock(return_value="formatted message")
    agent._build_seed_messages = AsyncMock(return_value=[
        {"role": "user", "content": "We should pick a cache database."},
        {"role": "assistant", "content": "Redis is the obvious fit."},
        {"role": "user", "content": "What database should we pick for the cache?"},
    ])
    agent._store_event_episode = AsyncMock(return_value=None)
    return agent


def _silence(note: str | None = _NOTE) -> SalienceDecision:
    return SalienceDecision(
        speak=False, score=None, reason=REASON_ALREADY_ANSWERED, reason_note=note,
    )


def _note_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [rec for rec in caplog.records if rec.getMessage() == _NOTE_EVENT]


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec for rec in caplog.records
        if getattr(rec, "audit", None) is True and rec.getMessage() == _AUDIT_EVENT
    ]


def _record_text(rec: logging.LogRecord) -> str:
    return " ".join(str(v) for v in rec.__dict__.values())


async def _run(
    monkeypatch: pytest.MonkeyPatch, decision: SalienceDecision, *, mode: str,
) -> SalienceOutcome | None:
    monkeypatch.setattr(_BID_PATH, AsyncMock(return_value=decision))
    return await run_salience_gate(_stub_agent(), _event(), _open_floor_decision(), mode=mode)


# ─── The one egress ──────────────────────────────────────────────────────────


class TestReasonNoteDebugRecord:
    async def test_silence_with_a_note_emits_one_debug_record(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            outcome = await _run(monkeypatch, _silence(), mode=MODE_BID)

        assert outcome == SalienceOutcome(silence=True)
        records = _note_records(caplog)
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.DEBUG
        assert getattr(rec, "reason_note") == _NOTE
        assert getattr(rec, "reason_code") == REASON_ALREADY_ANSWERED
        assert getattr(rec, "agent_id") == "ember-owl"
        assert getattr(rec, "channel_id") == "group:planning"

    async def test_the_record_is_not_an_audit_record(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """§E keeps two egress paths: the note rides the debug path only, so
        the record must not be tagged ``audit`` and the audit must stay
        note-free on the very same turn."""
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await _run(monkeypatch, _silence(), mode=MODE_BID)

        rec = _note_records(caplog)[0]
        assert getattr(rec, "audit", None) is None
        audits = _audit_records(caplog)
        assert len(audits) == 1
        assert not hasattr(audits[0], "reason_note")
        assert _NOTE not in _record_text(audits[0])

    async def test_plan_rung_emits_the_record_too(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await _run(monkeypatch, _silence(), mode=MODE_PLAN)

        assert len(_note_records(caplog)) == 1


# ─── Nowhere else ────────────────────────────────────────────────────────────


class TestNoRecordOffTheSuppressionPath:
    async def test_speak_verdict_emits_no_record(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A note on a speak verdict is not a silence reason; it egresses nowhere."""
        decision = SalienceDecision(
            speak=True, score=None, reason=REASON_ADDS_SUBSTANCE, reason_note=_NOTE,
        )
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            outcome = await _run(monkeypatch, decision, mode=MODE_BID)

        assert outcome is not None and outcome.silence is False
        assert _note_records(caplog) == []
        assert _NOTE not in " ".join(_record_text(r) for r in caplog.records)

    async def test_silence_without_a_note_emits_no_record(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await _run(monkeypatch, _silence(note=None), mode=MODE_BID)

        assert _note_records(caplog) == []

    async def test_scalar_rung_emits_no_record(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``mode: off`` is byte-for-byte the v0.3.8 scalar gate — no new
        egress, even if a decision somehow carried a note."""
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await _run(monkeypatch, _silence(), mode=MODE_OFF)

        assert _note_records(caplog) == []
        assert _NOTE not in " ".join(_record_text(r) for r in caplog.records)


# ─── The rendered line: DEBUG only ───────────────────────────────────────────


@pytest.fixture
def _rendered_log(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    """Rebuild ``configure_logging``'s chain and render to a buffer (mirrors
    ``test_salience_gate_deliberation_audit.py``)."""
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
    # ``configure_logging`` raises the root level; put it back for the suite.
    logging.getLogger().setLevel(logging.WARNING)


def _rendered(buf: io.StringIO, message: str) -> list[dict[str, Any]]:
    return [
        rec for rec in (
            json.loads(line) for line in buf.getvalue().strip().splitlines()
            if line.startswith("{")
        )
        if rec.get("message") == message
    ]


class TestRenderedEgress:
    async def test_at_debug_the_note_reaches_the_rendered_line(
        self, _rendered_log: io.StringIO, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl", level="DEBUG")
        await _run(monkeypatch, _silence(), mode=MODE_BID)

        lines = _rendered(_rendered_log, _NOTE_EVENT)
        assert len(lines) == 1
        assert lines[0]["level"] == "DEBUG"
        assert lines[0]["reason_note"] == _NOTE
        assert lines[0]["reason_code"] == REASON_ALREADY_ANSWERED
        assert "audit" not in lines[0]

    async def test_at_the_info_default_the_note_renders_nowhere(
        self, _rendered_log: io.StringIO, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The default agent log level is INFO: the audit line still renders,
        the note line does not, and the note text is on no rendered line."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        await _run(monkeypatch, _silence(), mode=MODE_BID)

        assert len(_rendered(_rendered_log, _AUDIT_EVENT)) == 1
        assert _rendered(_rendered_log, _NOTE_EVENT) == []
        assert _NOTE not in _rendered_log.getvalue()
