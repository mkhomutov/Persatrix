"""Tier-priority order pin (RFC 0011 §E + RFC 0021 §J + RFC 0026 §E).

Locks the order in which ``_inject_memory_context`` invokes the
v0.3.x tiers so a future caller refactor cannot silently re-order them.
The expected order is the canonical cross-RFC sequence:

    relationship summary
      → open commitments              (deferred to v0.4.0)
      → channel history               (CHANNEL_MESSAGE only)
      → facts                         (RFC 0026 PR 3)
      → episodic recall
      → recent notes
      → duration priors               (deferred to v0.4.0)

The two slots marked deferred ship empty in v0.3.x; assertions only pin
the five tiers actually present in the runtime.

PR #341 review M-1: the earlier shape of this file filtered
``add_section_order`` against a hard-coded four-tier list, silently
dropping ``facts_context`` from the assertion.  A future refactor that
moved facts to a different slot (e.g., after ``episodic_recall``)
slipped past this pin because the filter never saw the section name.
The fix wires a real :class:`FactStore` with one stored fact so the
section admits and the canonical five-tier order is asserted
end-to-end.  The TICK assertion mirrors the existing
``channel_history not in order`` shape and pins ``facts_context``
absent when ``event.sender_id`` is ``None`` (the ``_subject_seeds``
short-circuit).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType


@pytest.fixture
async def seeded_episodic() -> AsyncGenerator[EpisodicMemory, None]:
    """In-memory episodes that match every tier's recall query."""
    mem = EpisodicMemory(agent_id="priority-order-test", db_path=":memory:")
    await mem.initialize()
    try:
        # All entries stamped ``public`` so both harness events pass the
        # RFC 0037 §D gate: the TICK turn takes the acting ``public``
        # floor (rule (b)) and would withhold the ``internal`` default.
        # The gate itself is pinned in test_injection_gate.py; this file
        # pins tier ORDER only.
        # Channel-scoped episodes (channel-history tier).
        await mem.store_episode(
            summary="planning channel turn one rollout",
            context={},
            importance=0.9,
            scope="group:planning",
            protection_level="public",
        )
        # Generic episode that matches the CHANNEL_MESSAGE / TICK query
        # so the episodic-recall tier admits content (no-scope filter).
        await mem.store_episode(
            summary="rollout planning episode general",
            context={},
            importance=0.9,
            protection_level="public",
        )
        # Note matching the same query so the notes tier also admits.
        await mem.store_note(
            topic="rollout",
            content="rollout planning notes for the team review.",
            protection_level="public",
        )
        yield mem
    finally:
        await mem.close()


@pytest.fixture
async def seeded_fact_store() -> AsyncGenerator[FactStore, None]:
    """In-memory FactStore with one fact about ``agent-a``.

    The CHANNEL_MESSAGE event's ``sender_id`` is ``agent-a`` so this
    fact admits via :func:`_subject_seeds` and the priority pin can
    assert the ``facts_context`` slot end-to-end.
    """
    import time  # noqa: PLC0415 — local import keeps top-of-file shape

    store = FactStore(agent_id="agent-b", db_path=":memory:")
    await store.initialize()
    try:
        await store.store(
            subject="agent-a",
            predicate="prefers",
            object="rollout planning",
            source_interaction_id="i1",
            asserted_at=time.time(),
        )
        yield store
    finally:
        await store.close()


@dataclass
class _FakeRel:
    other_participant_id: str = "agent-a"
    other_participant_type: str = "agent"
    interaction_count: int = 5
    trust_score: float = 0.7
    notes: str | None = "agent-a is detail-oriented"
    last_interaction_at: float | None = 1.0
    first_interaction_at: float | None = 0.0


class _Mixin(_MemoryContextMixin):
    def __init__(self) -> None:
        from agents.clock import WallClock  # noqa: PLC0415 — local import

        super().__init__()
        self._clock = WallClock()
        self._timezone = "UTC"

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        if event.event_type is EventType.TICK:
            # Use a query that overlaps with seeded content so the tiers
            # actually admit.
            return "rollout planning"
        payload = getattr(event, "payload", {}) or {}
        return str(payload.get("content", "rollout planning"))


def _wire_mixin(
    episodic: EpisodicMemory,
    *,
    channel: bool,
    fact_store: FactStore | None = None,
) -> tuple[_Mixin, Any, list[str]]:
    mixin = _Mixin()
    mixin.agent_id = "agent-b"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = _FakeRel()
    # Wire the facts tier in too so the priority pin includes
    # ``facts_context``.  ``None`` keeps the legacy harness path
    # (recall short-circuits) for tests that only exercise the
    # other tiers.
    mixin._fact_store = fact_store

    add_section_order: list[str] = []
    real_add_section = mixin._working_memory.add_section

    def _record(section: Any) -> None:
        add_section_order.append(section.name)
        real_add_section(section)

    mixin._working_memory.add_section = _record  # type: ignore[assignment]

    event = MagicMock()
    event.event_type = EventType.CHANNEL_MESSAGE if channel else EventType.TICK
    event.channel_id = "group:planning" if channel else None
    event.sender_id = "agent-a" if channel else None
    event.thread_id = None
    # RFC 0037 §B: the production dispatch path stamps the channel's
    # classification onto the event; the harness mirrors it so the
    # channel turn acts ``internal`` (the fact fixture's default level).
    event.metadata = {"channel_classification": "internal"} if channel else {}
    event.payload = {
        "content": "rollout planning",
        "channel_type": "group" if channel else None,
    }
    event.timestamp = 0.0
    return mixin, event, add_section_order


async def test_priority_order_matches_rfc_0011_section_e_and_rfc_0021_section_j_for_channel_message(
    seeded_episodic: EpisodicMemory, seeded_fact_store: FactStore,
) -> None:
    """CHANNEL_MESSAGE → relationship → channel_history → facts → episodic_recall → recent_notes.

    Pins the canonical cross-RFC order against
    [RFC 0011 §E](docs/rfcs/0011-channels-bridges.md#e-memory-integration),
    [RFC 0021 §J](docs/rfcs/0021-persona-temporal-awareness.md#j-token-budget-integration),
    and [RFC 0026 §E](docs/rfcs/0026-declarative-facts-tier.md#e-composition-with-recall_notes).

    PR #341 review M-1: ``facts_context`` is now in the expected list so
    a future refactor that drops it from the allocate-loop (or moves it
    to a different slot) is caught by this regression pin.
    """
    mixin, event, order = _wire_mixin(
        seeded_episodic, channel=True, fact_store=seeded_fact_store,
    )
    await mixin._inject_memory_context(event)

    expected = [
        "relationship_context",
        "channel_history",
        "facts_context",
        "episodic_recall",
        "recent_notes",
    ]
    actual = [n for n in order if n in expected]
    assert actual == expected, (
        f"tier priority order drifted from RFC 0011 §E + RFC 0021 §J + "
        f"RFC 0026 §E. expected {expected}; got {actual}"
    )


async def test_priority_order_matches_rfc_0011_section_e_and_rfc_0021_section_j_for_tick(
    seeded_episodic: EpisodicMemory, seeded_fact_store: FactStore,
) -> None:
    """TICK → episodic_recall → recent_notes (no channel_history, relationship, or facts).

    TICK events have no sender so the relationship tier is skipped at
    the sender-id guard and the facts tier short-circuits inside
    :func:`agents.persona_runtime.facts_section._subject_seeds` (which
    returns ``[]`` for a ``None`` / whitespace-only sender).  The
    priority pin therefore covers the two remaining tiers, plus the
    absence of ``channel_history`` and ``facts_context`` (PR #341
    review M-1 — mirrors the pre-existing ``channel_history not in
    order`` assertion).
    """
    mixin, event, order = _wire_mixin(
        seeded_episodic, channel=False, fact_store=seeded_fact_store,
    )
    await mixin._inject_memory_context(event)

    expected = ["episodic_recall", "recent_notes"]
    actual = [n for n in order if n in expected]
    assert actual == expected, (
        f"non-channel tier priority order drifted. "
        f"expected {expected}; got {actual}"
    )
    assert "channel_history" not in order, (
        "channel_history must not be added on non-CHANNEL_MESSAGE events"
    )
    assert "facts_context" not in order, (
        "facts_context must not be added when event.sender_id is None — "
        "_subject_seeds short-circuit"
    )
