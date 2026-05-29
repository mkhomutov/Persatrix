"""ISSUE-0081 PR 3 — gRPC principal propagation + ``on_event`` binding.

The tenant analogue of ``test_session_id_pr2_binding.py`` Groups A/B.
Pins:

* :class:`TestPrincipalFromMetadata` — the ``_principal_from_metadata``
  helper lifts the ``persatrix-principal`` header (case-insensitive,
  empty-skipping, bytes-skipping) off gRPC metadata.
* :class:`TestRequestScopeFromMetadata` — the combined
  ``request_scope_from_metadata`` binds **both** the session and the
  principal from one event-metadata mapping, and is a no-op for whichever
  axis is absent.
* :class:`TestOnEventBindsPrincipal` — ``on_event`` enters the principal
  scope for the handler's lifetime when the event carries the principal
  metadata key (the same universal funnel both inbound paths use).
"""

from __future__ import annotations

import pytest

from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import (
    EVENT_PRINCIPAL_METADATA_KEY,
    PRINCIPAL_METADATA_GRPC_KEY,
    current_principal_id,
)
from agents.request_scope import request_scope_from_metadata
from agents.session_id import EVENT_SESSION_METADATA_KEY, current_session_id
from agents.session_metadata import _principal_from_metadata

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Group A — the pure metadata-lifting helper ─────────────


class TestPrincipalFromMetadata:
    def test_wire_key_constant_is_stable(self) -> None:
        assert PRINCIPAL_METADATA_GRPC_KEY == "persatrix-principal"

    def test_returns_value_when_present(self) -> None:
        assert _principal_from_metadata(
            [("persatrix-principal", "tenant-x")],
        ) == "tenant-x"

    def test_returns_none_when_absent(self) -> None:
        assert _principal_from_metadata([("user-agent", "grpc-go")]) is None

    def test_none_metadata_returns_none(self) -> None:
        assert _principal_from_metadata(None) is None

    def test_empty_value_is_skipped(self) -> None:
        # A blank header must not bind a blank scope (which would collapse
        # to the default principal); treat it as absent.
        assert _principal_from_metadata([("persatrix-principal", "")]) is None

    def test_key_match_is_case_insensitive(self) -> None:
        assert _principal_from_metadata(
            [("Persatrix-Principal", "tenant-c")],
        ) == "tenant-c"

    def test_bytes_value_is_skipped(self) -> None:
        assert _principal_from_metadata(
            [("persatrix-principal", b"tenant-x")],
        ) is None

    def test_event_envelope_key_is_namespaced(self) -> None:
        assert EVENT_PRINCIPAL_METADATA_KEY == "persatrix_principal"
        assert EVENT_PRINCIPAL_METADATA_KEY != PRINCIPAL_METADATA_GRPC_KEY


# ─── Group B — combined request-scope binder ────────────────


class TestRequestScopeFromMetadata:
    def test_binds_both_axes(self) -> None:
        md = {
            EVENT_SESSION_METADATA_KEY: "conv-x",
            EVENT_PRINCIPAL_METADATA_KEY: "tenant-x",
        }
        with request_scope_from_metadata(md):
            assert current_session_id() == "conv-x"
            assert current_principal_id() == "tenant-x"
        assert current_session_id() is None
        assert current_principal_id() is None

    def test_principal_only(self) -> None:
        md = {EVENT_PRINCIPAL_METADATA_KEY: "tenant-y"}
        with request_scope_from_metadata(md):
            assert current_session_id() is None
            assert current_principal_id() == "tenant-y"

    def test_neither_is_noop(self) -> None:
        with request_scope_from_metadata({}):
            assert current_session_id() is None
            assert current_principal_id() is None


# ─── Group C — on_event binds the principal for the handler ─


def _agent():
    return create_persona_agent(
        agent_id="ember-owl",
        config=_PERSONA_CONFIG,
        llm_client=_make_client(),
    )


class TestOnEventBindsPrincipal:
    async def test_event_metadata_binds_principal_scope(self) -> None:
        agent = _agent()
        captured: dict[str, str | None] = {}

        async def fake_inner(event: AgentEvent):
            captured["pid"] = current_principal_id()
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            metadata={EVENT_PRINCIPAL_METADATA_KEY: "tenant-x"},
        )
        await agent.on_event(event)
        assert captured.get("pid") == "tenant-x"

    async def test_no_metadata_leaves_principal_unset(self) -> None:
        agent = _agent()
        captured: dict[str, str | None] = {}

        async def fake_inner(event: AgentEvent):
            captured["pid"] = current_principal_id()
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        await agent.on_event(AgentEvent(event_type=EventType.TICK))
        assert captured.get("pid") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
