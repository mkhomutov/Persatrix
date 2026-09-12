"""Shared scaffold for the ``salience_gate`` seam tests (RFC 0030 Tier B / RFC 0051).

``test_salience_gate_deliberation_audit.py`` and
``test_salience_gate_reason_note_egress.py`` drive
:func:`agents.persona_runtime.salience_gate.run_salience_gate` with the same
open-floor admit, the same governed ``CHANNEL_MESSAGE`` and the same stub
agent, and read the same ``agent.deliberated`` audit records back — so that
scaffold lives here once (cf. ``_salience_reasoning_helpers`` for the pure-bid
tests). The rendered-line fixture the two files share is ``rendered_log`` in
``tests/unit/python/conftest.py``.

Importable as ``_salience_gate_helpers`` because ``tests/conftest.py`` puts
``tests/`` on ``sys.path``."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.persona_types import AgentEvent, EventType
from agents.response_gate import POLICY_ALWAYS, GateDecision

_AUDIT_EVENT = "agent.deliberated"

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
