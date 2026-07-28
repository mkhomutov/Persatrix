"""RFC 0037 §E — the §D gate's projection-selection branch (v0.3.12 PR 6).

Pins :func:`agents.persona_runtime.projection_branch.apply_episode_projections`
over a real in-memory store: highest-``≤ L`` selection, candidate-order
reinsertion, the rule-(c) entry case, the fail-to-withhold posture on
storage errors, and the manifest labeling the served projection at ITS
level (not the withheld entry's).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.memory.episode_types import Episode
from agents.memory.projections import (
    ENTRY_TIER_EPISODE,
    replace_entry_projections,
)
from agents.persona_runtime.injection_gate import TurnInjectionGate
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_runtime.projection_branch import apply_episode_projections

pytestmark = pytest.mark.asyncio


def _episode(
    episode_id: str,
    *,
    interaction_id: str | None,
    protection_level: str,
    summary: str = "verbatim protected text",
) -> Episode:
    return Episode(
        id=episode_id, agent_id="test-agent", summary=summary, context={},
        outcome=None, importance=0.5, access_count=0, last_accessed_at=None,
        tags=[], created_at=1.0, compressed_at=None, compression_level=0,
        interaction_id=interaction_id,
        protection_level=protection_level,
    )


def _gate(acting: str | None) -> TurnInjectionGate:
    return TurnInjectionGate(acting=acting, agent_id="test-agent")


async def _seed(memory, interaction_id: str, projections: dict) -> None:
    await replace_entry_projections(
        memory, entry_id=interaction_id, entry_tier=ENTRY_TIER_EPISODE,
        projections=projections, created_at=100.0,
    )


async def test_withheld_episode_served_as_its_projection(memory) -> None:
    await _seed(memory, "ix-1", {"public": "A roadmap decision was made."})
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="restricted"),
    ])
    assert admitted == []
    channel, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert channel == []
    assert [e.summary for e in episodic] == ["A roadmap decision was made."]
    assert episodic[0].protection_level == "public"
    assert episodic[0].id == "ep-1"


async def test_highest_level_at_or_below_acting_wins(memory) -> None:
    await _seed(memory, "ix-1", {
        "public": "public line", "internal": "internal line",
    })
    gate = _gate("internal")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="secret"),
    ])
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert [(e.protection_level, e.summary) for e in episodic] == [
        ("internal", "internal line"),
    ]


async def test_projection_above_acting_level_is_not_served(memory) -> None:
    """Only ``internal`` exists; the acting ``public`` turn stays blunt."""
    await _seed(memory, "ix-1", {"internal": "internal line"})
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="restricted"),
    ])
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert episodic == []


async def test_no_projection_stays_withheld(memory) -> None:
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="restricted"),
    ])
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert episodic == []


async def test_candidate_order_is_preserved(memory) -> None:
    """The replacement re-enters where the withheld original stood in the
    relevance ranking, between the two admitted entries."""
    await _seed(memory, "ix-mid", {"public": "the projected middle"})
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-a", interaction_id=None, protection_level="public",
                 summary="first"),
        _episode("ep-b", interaction_id="ix-mid",
                 protection_level="restricted"),
        _episode("ep-c", interaction_id=None, protection_level="public",
                 summary="last"),
    ])
    assert [e.id for e in admitted] == ["ep-a", "ep-c"]
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert [(e.id, e.summary) for e in episodic] == [
        ("ep-a", "first"),
        ("ep-b", "the projected middle"),
        ("ep-c", "last"),
    ]


async def test_channel_history_tier_is_served_too(memory) -> None:
    await _seed(memory, "ix-1", {"public": "projected history line"})
    gate = _gate("public")
    admitted = gate.filter_entries("channel_history", [
        _episode("ep-1", interaction_id="ix-1", protection_level="restricted"),
    ])
    channel, _ = await apply_episode_projections(
        gate, memory, channel_history=admitted, episodic_entries=[],
    )
    assert [e.summary for e in channel] == ["projected history line"]


async def test_rule_c_unknown_label_entry_can_still_project(memory) -> None:
    """A corrupted ENTRY label withholds the verbatim text (rule (c)),
    but the projection row carries its own valid level — serving it at
    ``≤ L`` discloses nothing the projection's level does not admit."""
    await _seed(memory, "ix-1", {"public": "safe abstraction"})
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="clasified"),
    ])
    assert gate.unknown_label_count == 1
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert [e.summary for e in episodic] == ["safe abstraction"]


async def test_storage_failure_degrades_to_the_blunt_withhold(memory) -> None:
    await _seed(memory, "ix-1", {"public": "never served"})
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="restricted"),
    ])
    with patch(
        "agents.persona_runtime.projection_branch.projections_for",
        side_effect=RuntimeError("db exploded"),
    ):
        _, episodic = await apply_episode_projections(
            gate, memory, channel_history=[], episodic_entries=admitted,
        )
    assert episodic == []


async def test_interaction_id_less_episode_is_skipped(memory) -> None:
    """A raw ``store_episode`` row (no close path, no interaction id) has
    no projection key — the branch must not crash or serve anything."""
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id=None, protection_level="restricted"),
    ])
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert episodic == []


async def test_nothing_withheld_returns_the_lists_unchanged(memory) -> None:
    gate = _gate("internal")
    entries = [
        _episode("ep-1", interaction_id="ix-1", protection_level="internal"),
    ]
    admitted = gate.filter_entries("episodic", entries)
    channel, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert episodic is admitted  # fast path — no lookup, no rebuild
    assert channel == []


async def test_manifest_labels_the_projection_at_its_own_level(memory) -> None:
    """§G honesty: what reached the prompt is the abstraction at the
    projection's level, and the manifest must say exactly that."""
    await _seed(memory, "ix-1", {"public": "projected"})
    gate = _gate("public")
    admitted = gate.filter_entries("episodic", [
        _episode("ep-1", interaction_id="ix-1", protection_level="restricted"),
    ])
    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    budget = MemoryBudget(total_tokens=1000)
    budget.try_add(episodic[0].summary)
    budget.record_admission(tier="episodic", item_id="ep-1", tokens_admitted=5)
    manifest = gate.manifest(budget)
    assert [(m.tier, m.entry_id, m.protection_level) for m in manifest] == [
        ("episodic", "ep-1", "public"),
    ]
    # The verbatim withhold still counted — the §D guarantee held.
    assert gate.withheld_count == 1
