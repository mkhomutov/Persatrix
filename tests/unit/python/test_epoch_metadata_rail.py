"""ISSUE-0085 PR 4 — gRPC epoch propagation + ``on_event`` binding.

The run/test-isolation analogue of ``test_principal_metadata_rail.py``
(and the session half of ``test_session_id_pr2_binding.py``).  Pins:

* :class:`TestEpochFromMetadata` — the ``_epoch_from_metadata`` helper
  lifts the ``persatrix-epoch`` header (case-insensitive, empty-skipping,
  bytes-skipping) off gRPC metadata.
* :class:`TestRequestScopeBindsEpoch` — the combined
  ``request_scope_from_metadata`` binds the epoch **alongside** the
  session and principal from one event-metadata mapping, and is a no-op
  for the epoch axis when its key is absent.
* :class:`TestOnEventBindsEpoch` — ``on_event`` enters the epoch scope
  for the handler's lifetime when the event carries the epoch metadata
  key (the same universal funnel both inbound paths use).

Unlike the principal rail (silent until RFC 0039), the epoch rail has a
live producer from day one — the orchestrator emits ``live`` in
production, a per-job id in CI — so this rail is exercised on every
request, not just once auth lands.
"""

from __future__ import annotations

import pytest

from agents.epoch_id import (
    EPOCH_METADATA_GRPC_KEY,
    EVENT_EPOCH_METADATA_KEY,
    current_epoch_id,
)
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import EVENT_PRINCIPAL_METADATA_KEY, current_principal_id
from agents.request_scope import request_scope_from_metadata
from agents.session_id import EVENT_SESSION_METADATA_KEY, current_session_id
from agents.session_metadata import _epoch_from_metadata

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Group A — the pure metadata-lifting helper ─────────────


class TestEpochFromMetadata:
    def test_wire_key_constant_is_stable(self) -> None:
        # Cross-language contract with grpcmeta.MDEpoch — a drift on either
        # side silently disables per-request epoch binding persona-side.
        assert EPOCH_METADATA_GRPC_KEY == "persatrix-epoch"

    def test_returns_value_when_present(self) -> None:
        assert _epoch_from_metadata(
            [("persatrix-epoch", "run-42")],
        ) == "run-42"

    def test_returns_none_when_absent(self) -> None:
        assert _epoch_from_metadata([("user-agent", "grpc-go")]) is None

    def test_none_metadata_returns_none(self) -> None:
        assert _epoch_from_metadata(None) is None

    def test_empty_value_is_skipped(self) -> None:
        # A blank header must not bind a blank scope (which would collapse
        # to the default epoch); treat it as absent.
        assert _epoch_from_metadata([("persatrix-epoch", "")]) is None

    def test_key_match_is_case_insensitive(self) -> None:
        assert _epoch_from_metadata(
            [("Persatrix-Epoch", "run-c")],
        ) == "run-c"

    def test_bytes_value_is_skipped(self) -> None:
        assert _epoch_from_metadata(
            [("persatrix-epoch", b"run-42")],
        ) is None

    def test_event_envelope_key_is_namespaced(self) -> None:
        assert EVENT_EPOCH_METADATA_KEY == "persatrix_epoch"
        assert EVENT_EPOCH_METADATA_KEY != EPOCH_METADATA_GRPC_KEY


# ─── Group B — combined request-scope binder ────────────────


class TestRequestScopeBindsEpoch:
    def test_binds_all_three_axes(self) -> None:
        md = {
            EVENT_SESSION_METADATA_KEY: "conv-x",
            EVENT_PRINCIPAL_METADATA_KEY: "tenant-x",
            EVENT_EPOCH_METADATA_KEY: "run-x",
        }
        with request_scope_from_metadata(md):
            assert current_session_id() == "conv-x"
            assert current_principal_id() == "tenant-x"
            assert current_epoch_id() == "run-x"
        assert current_session_id() is None
        assert current_principal_id() is None
        assert current_epoch_id() is None

    def test_epoch_only(self) -> None:
        md = {EVENT_EPOCH_METADATA_KEY: "run-y"}
        with request_scope_from_metadata(md):
            assert current_session_id() is None
            assert current_principal_id() is None
            assert current_epoch_id() == "run-y"

    def test_neither_is_noop(self) -> None:
        with request_scope_from_metadata({}):
            assert current_epoch_id() is None


# ─── Group C — on_event binds the epoch for the handler ─────


def _agent():
    return create_persona_agent(
        agent_id="ember-owl",
        config=_PERSONA_CONFIG,
        llm_client=_make_client(),
    )


class TestOnEventBindsEpoch:
    async def test_event_metadata_binds_epoch_scope(self) -> None:
        agent = _agent()
        captured: dict[str, str | None] = {}

        async def fake_inner(event: AgentEvent):
            captured["eid"] = current_epoch_id()
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            metadata={EVENT_EPOCH_METADATA_KEY: "run-42"},
        )
        await agent.on_event(event)
        assert captured.get("eid") == "run-42"

    async def test_no_metadata_leaves_epoch_unset(self) -> None:
        agent = _agent()
        captured: dict[str, str | None] = {}

        async def fake_inner(event: AgentEvent):
            captured["eid"] = current_epoch_id()
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        await agent.on_event(AgentEvent(event_type=EventType.TICK))
        assert captured.get("eid") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
