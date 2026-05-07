"""Tier-priority order pin (RFC 0011 §E + RFC 0021 §J).

Locks the order in which ``_inject_memory_context`` invokes the four
v0.3.0 tiers so a future caller refactor cannot silently re-order them.
The expected order is the canonical cross-RFC sequence:

    relationship summary
      → open commitments              (deferred to v0.4.0)
      → channel history               (CHANNEL_MESSAGE only)
      → episodic recall
      → recent notes
      → duration priors               (deferred to v0.4.0)

The two slots marked deferred ship empty in v0.3.0; assertions only pin
the four tiers actually present in the runtime.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType


@pytest.fixture
async def seeded_episodic() -> AsyncGenerator[EpisodicMemory, None]:
    """In-memory episodes that match every tier's recall query."""
    mem = EpisodicMemory(agent_id="priority-order-test", db_path=":memory:")
    await mem.initialize()
    try:
        # Channel-scoped episodes (channel-history tier).
        await mem.store_episode(
            summary="planning channel turn one rollout",
            context={},
            importance=0.9,
            scope="group:planning",
        )
        # Generic episode that matches the CHANNEL_MESSAGE / TICK query
        # so the episodic-recall tier admits content (no-scope filter).
        await mem.store_episode(
            summary="rollout planning episode general",
            context={},
            importance=0.9,
        )
        # Note matching the same query so the notes tier also admits.
        await mem.store_note(
            topic="rollout",
            content="rollout planning notes for the team review.",
        )
        yield mem
    finally:
        await mem.close()


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
    episodic: EpisodicMemory, *, channel: bool,
) -> tuple[_Mixin, Any, list[str]]:
    mixin = _Mixin()
    mixin.agent_id = "agent-b"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = _FakeRel()

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
    event.metadata = {}
    event.payload = {
        "content": "rollout planning",
        "channel_type": "group" if channel else None,
    }
    event.timestamp = 0.0
    return mixin, event, add_section_order


async def test_priority_order_matches_rfc_0011_section_e_and_rfc_0021_section_j_for_channel_message(
    seeded_episodic: EpisodicMemory,
) -> None:
    """CHANNEL_MESSAGE → relationship → channel_history → episodic_recall → recent_notes.

    Pins the canonical cross-RFC order against
    [RFC 0011 §E](docs/rfcs/0011-channels-bridges.md#e-memory-integration)
    and [RFC 0021 §J](docs/rfcs/0021-persona-temporal-awareness.md#j-token-budget-integration).
    """
    mixin, event, order = _wire_mixin(seeded_episodic, channel=True)
    await mixin._inject_memory_context(event)

    expected = [
        "relationship_context",
        "channel_history",
        "episodic_recall",
        "recent_notes",
    ]
    actual = [n for n in order if n in expected]
    assert actual == expected, (
        f"tier priority order drifted from RFC 0011 §E + RFC 0021 §J. "
        f"expected {expected}; got {actual}"
    )


async def test_priority_order_matches_rfc_0011_section_e_and_rfc_0021_section_j_for_tick(
    seeded_episodic: EpisodicMemory,
) -> None:
    """TICK → episodic_recall → recent_notes (no channel_history, no relationship).

    TICK events have no sender so the relationship tier is skipped at the
    sender-id guard; the priority pin therefore covers only the two
    remaining tiers, plus the absence of ``channel_history``.
    """
    mixin, event, order = _wire_mixin(seeded_episodic, channel=False)
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
