"""RFC 0037 §E projection storage — unit tests (v0.3.12 PR 6).

Pins the :mod:`agents.memory.projections` contract over the
``memory_projections`` table the PR 3 migration created dark:
replace-never-accumulate writes, the empty-text guard, the agent ACL
scope, and the caller-resolved level IN-set (§A rule (c) realised in
SQL — a corrupted stored level silently falls out of the read).
"""

from __future__ import annotations

import pytest

from agents.memory.projections import (
    ENTRY_TIER_EPISODE,
    projections_for,
    replace_entry_projections,
)

pytestmark = pytest.mark.asyncio


async def test_write_then_read_round_trips(memory) -> None:
    written = await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={
            "public": "A staffing decision was made.",
            "internal": "Leadership settled the sunset question.",
        },
        created_at=100.0,
    )
    assert written == 2
    got = await projections_for(
        memory,
        entry_tier=ENTRY_TIER_EPISODE,
        entry_ids=["ix-1"],
        levels=["public", "internal"],
    )
    assert got == {
        "ix-1": [
            ("internal", "Leadership settled the sunset question."),
            ("public", "A staffing decision was made."),
        ],
    }


async def test_reconsolidation_replaces_never_accumulates(memory) -> None:
    """A later set that DROPPED a level must not leave that level's
    stale text serving at the gate (the table's natural-key contract)."""
    await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={"public": "old public", "internal": "old internal"},
        created_at=100.0,
    )
    await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={"internal": "new internal"},
        created_at=200.0,
    )
    got = await projections_for(
        memory,
        entry_tier=ENTRY_TIER_EPISODE,
        entry_ids=["ix-1"],
        levels=["public", "internal"],
    )
    assert got == {"ix-1": [("internal", "new internal")]}


async def test_blank_text_is_not_written(memory) -> None:
    """The snippet maps "no safe restatement" to an empty string; the
    parser drops those, and the store guards again as a final line."""
    written = await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={"public": "   ", "internal": "kept"},
        created_at=100.0,
    )
    assert written == 1
    got = await projections_for(
        memory,
        entry_tier=ENTRY_TIER_EPISODE,
        entry_ids=["ix-1"],
        levels=["public", "internal"],
    )
    assert got == {"ix-1": [("internal", "kept")]}


async def test_level_in_set_filters_rule_c_in_sql(memory) -> None:
    """A stored level outside the caller's IN-set — the corrupted-label
    case included — falls out of the read: rule (c) in SQL."""
    db = memory._ensure_db()
    await db.execute(
        "INSERT INTO memory_projections "
        "(agent_id, entry_id, entry_tier, level, text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("test-agent", "ix-1", ENTRY_TIER_EPISODE, "clasified", "oops", 1.0),
    )
    await replace_entry_projections(
        memory,
        entry_id="ix-2",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={"internal": "above the acting floor"},
        created_at=100.0,
    )
    got = await projections_for(
        memory,
        entry_tier=ENTRY_TIER_EPISODE,
        entry_ids=["ix-1", "ix-2"],
        levels=["public"],
    )
    assert got == {}


async def test_tier_and_id_scoping(memory) -> None:
    await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={"public": "episode projection"},
        created_at=100.0,
    )
    await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier="note",
        projections={"public": "note projection"},
        created_at=100.0,
    )
    got = await projections_for(
        memory,
        entry_tier=ENTRY_TIER_EPISODE,
        entry_ids=["ix-1", "ix-absent"],
        levels=["public"],
    )
    assert got == {"ix-1": [("public", "episode projection")]}


async def test_agent_scope_is_the_acl_axis(memory) -> None:
    """Rows written under another agent id never serve this agent's
    gate (the RFC 0008 §H ACL axis the ``agent_id`` column carries)."""
    db = memory._ensure_db()
    await db.execute(
        "INSERT INTO memory_projections "
        "(agent_id, entry_id, entry_tier, level, text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("other-agent", "ix-9", ENTRY_TIER_EPISODE, "public", "not yours", 1.0),
    )
    got = await projections_for(
        memory,
        entry_tier=ENTRY_TIER_EPISODE,
        entry_ids=["ix-9"],
        levels=["public"],
    )
    assert got == {}


async def test_replace_never_clobbers_another_agents_rows(memory) -> None:
    """The DELETE is agent-scoped like the read: in a shared DB, a
    neighbour's rows for a colliding entry id survive this agent's
    re-consolidation of the same ``(entry_id, entry_tier)``."""
    db = memory._ensure_db()
    await db.execute(
        "INSERT INTO memory_projections "
        "(agent_id, entry_id, entry_tier, level, text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("other-agent", "ix-1", ENTRY_TIER_EPISODE, "internal", "theirs", 1.0),
    )
    await replace_entry_projections(
        memory,
        entry_id="ix-1",
        entry_tier=ENTRY_TIER_EPISODE,
        projections={"public": "mine"},
        created_at=100.0,
    )
    async with db.execute(
        "SELECT agent_id, level, text FROM memory_projections "
        "WHERE entry_id = ? ORDER BY agent_id",
        ("ix-1",),
    ) as cursor:
        rows = [tuple(row) async for row in cursor]
    assert rows == [
        ("other-agent", "internal", "theirs"),
        ("test-agent", "public", "mine"),
    ]


async def test_empty_inputs_short_circuit(memory) -> None:
    assert await projections_for(
        memory, entry_tier=ENTRY_TIER_EPISODE, entry_ids=[], levels=["public"],
    ) == {}
    assert await projections_for(
        memory, entry_tier=ENTRY_TIER_EPISODE, entry_ids=["ix-1"], levels=[],
    ) == {}
