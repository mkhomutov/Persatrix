"""RFC 0037 PR 5 — the ``recall_channel_messages`` §F acting-level binding.

Split from ``test_recall_tool.py`` (which pins the RFC 0036 client / factory
/ wiring contracts and sits at the repo's 500-line cap) — the same carve the
production module documents for its lazy lattice import.  Pins the one thing
PR 5 adds to the TOOL: the acting classification is bound from the turn's
task-local scope (:mod:`agents.acting_classification` — the trusted
event/floor resolution seam), resolved through §A rule (b)
(:func:`agents.persona_runtime.classification.normalize_acting`), and passed
to the client's required ``acting_classification`` parameter — never taken
from an LLM argument.  The endpoint-side validation and the SQL cap are
pinned Go-side (``persona_recall_handlers_classification_test.go`` /
``sqlite_search_classification_test.go``).
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.acting_classification import acting_classification_scope_from_metadata
from agents.channel_event_classification import CHANNEL_CLASSIFICATION_METADATA_KEY
from agents.tools.permissions import PermissionGate
from agents.tools.recall import create_recall_tool
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts and ends with an empty global tool registry —
    ``create_recall_tool`` registers ``recall_channel_messages`` by name."""
    clear_registry()
    yield
    clear_registry()


class _FakeRecallClient:
    """Duck-typed recall client — records calls, returns a canned result.
    (Self-contained duplicate of the ``test_recall_tool.py`` fake: the unit
    test modules here deliberately do not import from one another.)"""

    def __init__(self, result: list[dict[str, Any]] | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def recall(
        self, *, participant_id: str, acting_classification: str, query: str,
        channel_id: str = "", sender: str = "", limit: int = 10,
    ) -> list[dict[str, Any]] | None:
        self.calls.append({
            "participant_id": participant_id,
            "acting_classification": acting_classification, "query": query,
            "channel_id": channel_id, "sender": sender, "limit": limit,
        })
        return self._result


def _gate_recall() -> PermissionGate:
    return PermissionGate({"channels": {"recall": True}})


class TestRecallToolActingClassification:
    """RFC 0037 §F (PR 5): the tool binds the ACTING level from the turn's
    task-local classification scope — the trusted event/floor resolution seam
    (:mod:`agents.acting_classification`) — never from an LLM argument, and
    resolves it through rule (b) before it reaches the endpoint's required
    ``acting_classification`` parameter."""

    async def test_unbound_turn_floors_to_public(self):
        """No binding (an autonomous tick, a direct call outside any event
        scope) → the ``public`` floor, so a channel-less recall serves only
        public-classified channels rather than 400-ing."""
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        await td.func(query="x")
        assert client.calls[0]["acting_classification"] == "public"

    async def test_bound_channel_level_is_passed_verbatim(self):
        """A turn acting in a classified channel recalls at that channel's
        level — bound via the same metadata seam the ingress seeds."""
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        with acting_classification_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: "restricted"},
        ):
            await td.func(query="x")
        assert client.calls[0]["acting_classification"] == "restricted"

    async def test_unknown_bound_level_floors_to_public(self):
        """The contextvar carries the VERBATIM wire value (the seed applies
        no vocabulary check), so a garbage stamp from a skewed producer must
        floor here — rule (b) — rather than reach the endpoint and 400."""
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        with acting_classification_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: "sekrit"},
        ):
            await td.func(query="x")
        assert client.calls[0]["acting_classification"] == "public"

    def test_acting_level_is_not_an_llm_parameter(self):
        """The tool schema must not expose the acting level — it is bound
        from trusted context, and an LLM-supplied override would let the
        model recall above its turn's classification."""
        td = create_recall_tool(_FakeRecallClient([]), _gate_recall(), agent_id="ember-owl")
        assert "acting_classification" not in td.parameters.get("properties", {})
