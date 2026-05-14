"""RFC 0026 PR 2 — facts extractor wired into the close path.

Pins the PR 2 deliverables called out in ``docs/rfcs/0026-pr-plan.md``
§PR 2: one combined LLM call at interaction-close emits both the prose
summary *and* a JSON list of declarative-fact tuples, the summary
commits the same way RFC 0020 PR 4 already wires, and the facts land
in :class:`agents.memory.facts.FactStore` keyed by the local agent id.

Contracts asserted here:

* ``MemoryNamespace`` exposes a ``facts: FactStore`` slot that the
  persona runtime constructs and tears down in lock-step with the
  other tiers (no separate lifecycle).
* When the combined LLM response carries a well-formed
  ``{"summary": "...", "facts": [...]}`` payload, the episode summary
  column matches the summary half and the ``facts`` table holds one
  row per parsed tuple.
* A malformed ``facts`` JSON commits the summary (RFC 0026 §Phase 1
  step 4 rollback policy) and increments
  ``agent.facts.extraction_failed`` exactly once.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from _otel_test_helpers import counter_total

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._summarize_close_helpers import (
    PERSONA_CONFIG,
    drain,
    episode_summary,
    send_n_turns,
)


SUMMARY_TEXT = (
    "Bob mentioned his daughter Mira and his preference for tea."
)

# Two declarative facts the prompt is expected to extract from the
# above conversation; mirrors the canonical example in the dementia
# test (MT-MEMORY-005).
FACTS_PAYLOAD = [
    {
        "subject": "bob",
        "predicate": "has_child_named",
        "object": "Mira",
        "certainty": 0.95,
    },
    {
        "subject": "bob",
        "predicate": "prefers",
        "object": "tea",
        "certainty": 0.8,
    },
]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


def _make_combined_client(
    *,
    summary: str = SUMMARY_TEXT,
    facts: list[dict] | None = None,
    facts_raw: str | None = None,
) -> LLMClient:
    """Mock LLM that returns ``{"summary": ..., "facts": [...]}`` on the
    summariser call (``max_tokens == 256``).

    ``facts_raw`` overrides the serialised facts payload — used by the
    malformed-JSON path test to inject a broken ``facts`` block while
    keeping the rest of the JSON envelope valid.
    """
    if facts_raw is None:
        facts_raw = json.dumps(facts if facts is not None else [])
    payload = (
        '{"summary": ' + json.dumps(summary) + ', "facts": ' + facts_raw + "}"
    )

    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if max_tokens == 256:
            return LLMResponse(
                text=payload,
                stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def _make_agent(client: LLMClient) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=PERSONA_CONFIG["id"],
        config=PERSONA_CONFIG,
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


# ─── MemoryNamespace exposes facts ──────────────────────────


@pytest.mark.asyncio
class TestMemoryNamespaceFacts:
    async def test_facts_attribute_exposed_after_initialize(self):
        agent = await _make_agent(_make_combined_client(facts=[]))
        try:
            assert agent.memory.facts is not None
            # FactStore is initialised — a recall round-trip works
            # without raising "FactStore not initialised".
            assert await agent.memory.facts.recall(subject="bob") == []
        finally:
            await agent.close_memory()


# ─── Summary + facts both commit on the happy path ──────────


@pytest.mark.asyncio
class TestExtractorHappyPath:
    async def test_summary_matches_payload_and_facts_persisted(self):
        agent = await _make_agent(
            _make_combined_client(summary=SUMMARY_TEXT, facts=FACTS_PAYLOAD),
        )
        try:
            peer = "bob"
            await send_n_turns(agent, peer, 4)
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "thanks, bye"},
                sender_id=peer,
                metadata={"chat_end": True},
            ))
            await drain(agent)

            # Summary half — RFC 0020 PR 4 contract holds.
            summary = await episode_summary(agent)
            assert summary == SUMMARY_TEXT
            assert summary != SUMMARY_UNAVAILABLE_TEXT
            assert summary != SUMMARY_PENDING_TEXT

            # Facts half — both tuples landed in FactStore.
            live = await agent.memory.facts.recall(subject="bob")
            predicates = {f.predicate: f for f in live}
            assert set(predicates.keys()) == {"has_child_named", "prefers"}
            assert predicates["has_child_named"].object == "Mira"
            assert predicates["prefers"].object == "tea"
            # source_interaction_id was threaded by the close-path.
            for fact in live:
                assert fact.source_interaction_id  # non-empty
        finally:
            await agent.close_memory()

    async def test_empty_facts_list_commits_summary_only(self):
        """Short interactions emit ``facts: []`` — summary still writes;
        no extraction-failure counter increment."""
        reader, metrics_mod = _build_meter()
        try:
            agent = await _make_agent(
                _make_combined_client(summary=SUMMARY_TEXT, facts=[]),
            )
            try:
                peer = "bob"
                await send_n_turns(agent, peer, 2)
                await agent.on_event(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": "bye"},
                    sender_id=peer,
                    metadata={"chat_end": True},
                ))
                await drain(agent)

                summary = await episode_summary(agent)
                assert summary == SUMMARY_TEXT
                # No facts.
                assert await agent.memory.facts.recall(subject="bob") == []
                # extraction_failed counter did NOT increment.
                assert counter_total(
                    reader, "agent.facts.extraction_failed",
                ) == 0
            finally:
                await agent.close_memory()
        finally:
            await metrics_mod.shutdown()


# ─── Malformed facts JSON: summary commits, counter increments ──


@pytest.mark.asyncio
class TestExtractorPartialFailure:
    async def test_malformed_facts_commits_summary_and_increments_counter(self):
        """RFC 0026 §Phase 1 step 4 — facts-only parse failure does not
        abort the summary write.  The ``agent.facts.extraction_failed``
        counter surfaces the failure to operators."""
        reader, metrics_mod = _build_meter()
        try:
            agent = await _make_agent(
                _make_combined_client(
                    summary=SUMMARY_TEXT,
                    # Valid envelope JSON; ``facts`` half is the wrong
                    # shape (number, not a list of tuples) so the
                    # outer split recovers the summary but
                    # ``parse_facts_payload`` raises and the rollback
                    # policy increments ``agent.facts.extraction_failed``.
                    facts_raw="42",
                ),
            )
            try:
                peer = "bob"
                await send_n_turns(agent, peer, 4)
                await agent.on_event(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": "bye"},
                    sender_id=peer,
                    metadata={"chat_end": True},
                ))
                await drain(agent)

                # Summary still landed.
                summary = await episode_summary(agent)
                assert summary == SUMMARY_TEXT
                assert summary != SUMMARY_UNAVAILABLE_TEXT
                # No facts.
                assert await agent.memory.facts.recall(subject="bob") == []
                # extraction_failed bumped exactly once for the close.
                assert counter_total(
                    reader, "agent.facts.extraction_failed",
                ) == 1
            finally:
                await agent.close_memory()
        finally:
            await metrics_mod.shutdown()
