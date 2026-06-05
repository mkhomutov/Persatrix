"""Unit tests for ``render_episodic_section`` (v0.3.7 F-4 slice B
extraction).

The episodic tier was the last recall tier still rendered inline in
``_inject_memory_context``; the relationship / channel-history / facts /
notes tiers are all ``render_*`` helpers. Slice B extracts it into
``episodic_section.render_episodic_section`` — both to uniformize the
tiers and to free room in ``memory_context.py`` (at the 500-line cap) for
the channel-roster injection. This is a behaviour-preserving move; the
``_inject_memory_context`` integration is guarded by
``test_memory_context_priority_order`` and friends. These tests pin the
extracted helper directly: section name/priority/compressibility, the
``[recency-tag] summary`` line shape, budget admission, and the MQ-11
``record_admission(tier="episodic")`` provenance.
"""

from __future__ import annotations

from agents.memory.episodic_queries import Episode
from agents.persona_runtime.episodic_section import (
    EPISODIC_SECTION_NAME,
    EPISODIC_SECTION_PRIORITY,
    render_episodic_section,
)
from agents.persona_runtime.memory_budget import MemoryBudget


def _ep(
    id: str,
    summary: str,
    *,
    created_at: float = 1000.0,
    closed_at: float | None = None,
    started_at: float | None = None,
    turn_count: int | None = None,
) -> Episode:
    """A real :class:`Episode` with the fields the renderer reads; the rest
    take simple defaults."""
    return Episode(
        id=id, agent_id="a", summary=summary, context={}, outcome=None,
        importance=0.5, access_count=0, last_accessed_at=None, tags=[],
        created_at=created_at, compressed_at=None, compression_level=0,
        started_at=started_at, closed_at=closed_at, turn_count=turn_count,
    )


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class TestRenderEpisodicSection:
    def test_none_when_no_episodes(self) -> None:
        budget = MemoryBudget(total_tokens=1000)
        assert render_episodic_section(
            [], budget, now=2000.0, timezone="UTC", truncate=_truncate,
        ) is None

    def test_renders_section_with_recency_tagged_items(self) -> None:
        budget = MemoryBudget(total_tokens=1000)
        episodes = [
            _ep(id="e1", summary="shipped the widget", created_at=1500.0),
            _ep(id="e2", summary="planned the rollout", created_at=1000.0),
        ]
        section = render_episodic_section(
            episodes, budget, now=2000.0, timezone="UTC", truncate=_truncate,
        )
        assert section is not None
        assert section.name == EPISODIC_SECTION_NAME
        assert section.priority == EPISODIC_SECTION_PRIORITY
        assert section.compressible is True
        assert section.token_count > 0
        assert section.content.startswith("Relevant past episodes:")
        assert "shipped the widget" in section.content
        assert "planned the rollout" in section.content
        # Recency-tag prefix shape.
        assert "[" in section.content and "]" in section.content

    def test_records_episodic_provenance_for_admitted_items(self) -> None:
        """MQ-11: admitted episodes land on the per-turn provenance
        registry under the ``episodic`` tier (used by RFC 0026 PR 4).
        """
        budget = MemoryBudget(total_tokens=1000)
        episodes = [_ep(id="e1", summary="a thing happened")]
        render_episodic_section(
            episodes, budget, now=2000.0, timezone="UTC", truncate=_truncate,
        )
        assert budget.admissions_by_tier("episodic") == ["e1"]

    def test_budget_exhaustion_drops_tail(self) -> None:
        # A tiny budget admits at most the first item.
        budget = MemoryBudget(total_tokens=12)
        episodes = [
            _ep(id="e1", summary="first episode summary text"),
            _ep(id="e2", summary="second episode summary text"),
        ]
        section = render_episodic_section(
            episodes, budget, now=2000.0, timezone="UTC", truncate=_truncate,
        )
        admitted = budget.admissions_by_tier("episodic")
        # Never more than was offered; the tail is dropped under pressure.
        assert admitted in (["e1"], [])
        if section is not None:
            assert "second episode" not in section.content
