"""Deterministic closed-interaction recall order — :mod:`agents.memory.episodic_closed`.

Guards the ``rowid`` insertion-order tiebreak on
:func:`agents.memory.episodic_closed.recall_closed_interactions`'s
``closed_at DESC`` sort (``agents/memory/episodic_closed.py``). Without it,
closed interactions sharing a ``closed_at`` recall in SQLite-implementation-
defined order — one governance interaction maps to several episodes closed
together (ISSUE-0102), and the eval driver's FrozenClock closes in one instant —
which would make a recorded RFC 0044 golden's assembled prompt non-portable.

Split into its own file (mirrors ``test_fact_store_recall_order.py``) to keep
``test_closed_interactions_read.py`` under the 500-line size gate.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.episodic_closed import closed_interactions

# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def memory():
    """EpisodicMemory against an in-memory SQLite DB (mirrors the read tests)."""
    mem = EpisodicMemory(agent_id="agent-x", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


# ─── Deterministic tiebreak ─────────────────────────────────


async def test_recall_equal_closed_at_ordered_deterministically(memory):
    """Closed interactions sharing a ``closed_at`` recall most-recently-inserted
    first — the ``rowid`` (insertion-order) tiebreak on ``closed_at DESC``.

    ``episodes.id`` is a random uuid4, so without the tiebreak SQLite's order
    among equal ``closed_at`` is implementation-defined; because the recall is
    ``LIMIT``-ed, a tie at the cutoff would change *which* rows surface, not just
    their order, making a recorded RFC 0044 golden non-portable. Both rows are
    stored at one ``closed_at`` (mirrors a governance interaction's episodes
    closing together / the FrozenClock); insertion order is preserved by
    ``rowid``, so the newest-inserted must recall first."""
    for iid, summary, started in (
        ("first", "s1", 10.0),
        ("second", "s2", 20.0),
    ):
        await memory.store_episode(
            summary=summary,
            context={"scope": "group:a"},
            interaction_id=iid,
            started_at=started,
            closed_at=500.0,  # identical instant
            turn_count=3,
            scope="group:a",
        )

    rows = await closed_interactions(memory, limit=10)
    ids = [ep.interaction_id for ep in rows]
    assert ids == ["second", "first"], (
        "equal-closed_at interactions must recall most-recently-inserted first "
        "(rowid DESC tiebreak), not SQLite's implementation-defined order"
    )
    # Stable across repeated calls — the property a golden's request hash needs.
    again = await closed_interactions(memory, limit=10)
    assert [ep.interaction_id for ep in again] == ids
