"""RFC 0021 PR 2 — temporal telemetry counter accuracy tests (PR #260 review M-1).

Pins the ``agent.temporal.recency.rendered`` counter contract:

* The counter increments **once per admitted item**, not once per recall-set
  item.  When the memory budget drops episodes or relationship summaries,
  the counter must not overcount — operators correlating this metric against
  admitted token totals must see a consistent number.

Tests use a tight ``monkeypatch`` budget so drops are deterministic, and read
back the emitted counter via :class:`InMemoryMetricReader` so the assertion is
on the OTEL data plane, not on a mock call count.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.observability import metrics as pmetrics
from agents.persona_runtime import memory_context as memory_context_module
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._temporal_test_helpers import FROZEN_EPOCH, make_agent


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """Spin up an in-memory OTEL meter so counter emissions are observable.

    Mirrors the pattern in ``tests/integration/test_shared_pool_metrics.py``.
    """
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _collect_counter_points(
    reader: InMemoryMetricReader, metric_name: str,
) -> list[tuple[int, dict[str, Any]]]:
    """Return ``[(value, attributes)]`` for *metric_name*."""
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != metric_name:
                    continue
                for dp in getattr(m.data, "data_points", []):
                    out.append((dp.value, dict(dp.attributes)))
    return out


class TestRecencyCounterAccuracy:
    """``agent.temporal.recency.rendered`` counts admissions, not attempts.

    PR #260 review M-1: the episode-tier increment was previously
    ``add(len(episodes))``, which counted recall-set size rather than
    items that actually reached the prompt.  When the memory budget
    drops items (high-volume relationship + episodes + notes), operators
    correlating "recency tags emitted to the LLM" against admitted
    token totals would see a phantom delta.

    These tests pin the post-fix contract: the counter increments by
    one for each item that ``MemoryBudget.try_add`` admits, in both the
    episode and relationship tiers.
    """

    async def _store_episode(
        self,
        agent,
        *,
        summary: str,
        created_at_offset_sec: float = 0.0,
    ) -> None:
        ep_id = await agent._episodic_memory.store_episode(
            summary=summary,
            context={},
            importance=0.9,
        )
        if created_at_offset_sec:
            db = agent._episodic_memory._ensure_db()  # noqa: SLF001 — test-only
            await db.execute(
                "UPDATE episodes SET created_at = ? WHERE id = ?",
                (FROZEN_EPOCH + created_at_offset_sec, ep_id),
            )
            await db.commit()

    async def test_episode_counter_only_counts_admitted_items(
        self, monkeypatch: pytest.MonkeyPatch, metric_reader: InMemoryMetricReader,
    ) -> None:
        # Tighten the global memory budget so most episodes are dropped
        # by ``MemoryBudget.try_add``.  64 tokens leaves room for ~1
        # episode at the ``MIN_TOKENS_EPISODIC=32`` floor.  The episode
        # summaries below render to ~50 tokens each (long enough not to
        # be admittable as a chunk smaller than the floor, short enough
        # that one fits).
        monkeypatch.setattr(
            memory_context_module, "MEMORY_BUDGET_TOKENS", 64,
        )

        agent = await make_agent()
        try:
            for i in range(5):
                await self._store_episode(
                    agent,
                    summary=(
                        f"Episode {i}: " + "discussed roadmap with the leads " * 5
                    ),
                    created_at_offset_sec=-180 - i,
                )
            await agent._inject_memory_context(
                AgentEvent(event_type=EventType.TICK), query="roadmap",
            )

            section = agent._working_memory.get_section("episodic_recall")
            # Admitted episodes appear as ``- [recency] ...`` lines under
            # the ``Relevant past episodes:`` header.  Each item is
            # preceded by ``\n- `` (including the first, since the header
            # ends with ``:\n``), so ``count("\n- ")`` directly gives the
            # admitted count.  When the budget drops everything,
            # ``section`` is None and admitted == 0.
            admitted = (
                section.content.count("\n- ") if section is not None else 0
            )
            assert admitted < 5, (
                "test setup error: budget should drop at least one episode "
                f"to exercise the M-1 path, got admitted={admitted}"
            )

            points = _collect_counter_points(
                metric_reader, "agent.temporal.recency.rendered",
            )
            episode_points = [
                (v, a) for v, a in points if a.get("source") == "episode"
            ]
            episode_total = sum(v for v, _ in episode_points)
            # The fix: counter equals admitted, not the recall-set size
            # (which was 5).  Pre-fix this asserted 5; post-fix it equals
            # the number of items that actually reached the prompt.
            assert episode_total == admitted, (
                f"counter must reflect admitted items, not attempts: "
                f"counter={episode_total}, admitted={admitted}, recall_set=5"
            )
        finally:
            await agent.close_memory()

    async def test_relationship_counter_only_counts_admitted_summary(
        self, monkeypatch: pytest.MonkeyPatch, metric_reader: InMemoryMetricReader,
    ) -> None:
        # The relationship tier shares the same "increment-on-attempt"
        # shape pre-fix: the counter was incremented when ``Last seen``
        # was rendered into ``rel_lines``, regardless of whether the
        # composed ``rel_text`` was admitted.  Drop the budget below
        # ``MIN_TOKENS_RELATIONSHIP=64`` so the section is dropped
        # entirely and assert the counter stays at zero.
        monkeypatch.setattr(
            memory_context_module, "MEMORY_BUDGET_TOKENS", 16,
        )

        agent = await make_agent()
        try:
            await agent._relationship_memory.record_interaction(
                "alice", "chat", outcome="ok",
            )
            db = agent._relationship_memory._ensure_db()  # noqa: SLF001 — test-only
            await db.execute(
                "UPDATE relationships SET last_interaction_at = ? "
                "WHERE other_participant_id = 'alice'",
                (FROZEN_EPOCH - 3 * 86_400,),
            )
            await db.commit()

            event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "hi"},
                sender_id="alice",
                metadata={"sender_participant_type": "agent"},
            )
            await agent._inject_memory_context(event, query="hi")

            section = agent._working_memory.get_section("relationship_context")
            assert section is None, (
                "test setup error: 16-token budget should drop the "
                "relationship section so the M-1 contract can be observed"
            )

            points = _collect_counter_points(
                metric_reader, "agent.temporal.recency.rendered",
            )
            relationship_points = [
                (v, a) for v, a in points if a.get("source") == "relationship"
            ]
            relationship_total = sum(v for v, _ in relationship_points)
            assert relationship_total == 0, (
                "counter must not increment when the relationship section "
                f"was dropped by the budget; got {relationship_total}"
            )
        finally:
            await agent.close_memory()


class TestChannelHistoryRecencyCounter:
    """``agent.temporal.recency.rendered`` covers the channel-history tier.

    PR #264 review M-2: the channel-history tier (RFC 0011 §E + RFC 0021
    §J) renders the same ``[recency-tag] summary`` shape as the episodic
    tier, so the counter description ("Recency tags rendered onto
    recalled episodes or relationship summaries") applies — but the
    extracted ``render_channel_history_section`` initially shipped
    without the bump.  Operators correlating
    ``agent.temporal.recency.rendered`` against admitted token totals on
    an agent active in channels would see a phantom shortfall: real
    renders vs reported count.

    This test pins the contract: the counter increments by one per
    admitted channel-history item with ``source="channel_history"``,
    matching the episode and relationship paths.
    """

    async def test_channel_history_counter_only_counts_admitted_items(
        self, monkeypatch: pytest.MonkeyPatch, metric_reader: InMemoryMetricReader,
    ) -> None:
        # Tighten the budget so the recall set (5 channel-scoped
        # episodes) over-fills the budget and the channel-history loop
        # admits a strict subset.  Same shape as the episode tier test
        # above: the assertion is "counter == admitted", which catches
        # both the "no bump at all" failure mode and the future
        # "bump-before-admission-check" regression that would inflate the
        # counter back to recall-set size.
        monkeypatch.setattr(
            memory_context_module, "MEMORY_BUDGET_TOKENS", 96,
        )

        agent = await make_agent()
        try:
            for i in range(5):
                await agent._episodic_memory.store_episode(
                    summary=(
                        f"Planning channel {i}: "
                        + "discussed roadmap with the leads " * 5
                    ),
                    context={},
                    importance=0.9,
                    scope="group:planning",
                )
            event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                channel_id="group:planning",
                sender_id="alice",
                payload={"content": "hello", "channel_type": "group"},
            )
            await agent._inject_memory_context(event, query="hello")

            section = agent._working_memory.get_section("channel_history")
            # ``Recent channel turns:\n- ...`` — count the per-item
            # ``\n- `` separators (the header itself ends with ``:\n``,
            # so this also matches the first item).
            admitted = (
                section.content.count("\n- ") if section is not None else 0
            )
            assert 0 < admitted < 5, (
                "test setup error: budget should admit some-but-not-all "
                f"of the 5 seeded channel episodes to exercise the "
                f"admit-vs-attempt distinction, got admitted={admitted}"
            )

            points = _collect_counter_points(
                metric_reader, "agent.temporal.recency.rendered",
            )
            channel_points = [
                (v, a) for v, a in points if a.get("source") == "channel_history"
            ]
            channel_total = sum(v for v, _ in channel_points)
            assert channel_total == admitted, (
                "counter must reflect admitted channel-history items, not "
                f"recall-set size: counter={channel_total}, "
                f"admitted={admitted}, recall_set=5"
            )
        finally:
            await agent.close_memory()
