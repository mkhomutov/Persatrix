"""ISSUE-0130 shape (b) — "has this replayed span already been derived?"

One query, split out of :mod:`agents.memory.episodic_queries` because that
module was three lines under the 500-line cap and this is a self-contained
question with a contract worth stating.

The lookup is deliberately **not** principal-filtered, unlike every recall
path in this package.  Two reasons, and neither is an exception to the
tenant boundary:

* the interaction id it matches on is a digest that already contains the
  principal (:func:`agents.persona_runtime.replay_identity
  .replay_span_identity`), so a filter would restate the key rather than
  narrow it; and
* it discloses nothing.  A caller can only ask about a digest it has
  already computed, and the answer is a boolean about its own write.

It runs on the WRITE path only — the close path asks it before deriving,
so a span replayed on a later boot does not summarise itself again.  It
must never grow a read-side caller: `messages.principal_id` is public
(``policyPublic`` history), and a recall predicate keyed on it would turn a
public read into a tenant-selectable one, which ISSUE-0130 rules out.
"""

from __future__ import annotations

import aiosqlite

__all__ = ["episode_exists_for_interaction"]


async def episode_exists_for_interaction(
    db: aiosqlite.Connection, agent_id: str, interaction_id: str,
) -> bool:
    """``True`` iff ``agent_id`` already has an episode for this interaction.

    Any lifecycle state counts, including a ``[summary pending]`` row a
    crash left behind and one the janitor finalised to
    ``[summary unavailable]``: the question is whether this span was
    already turned into a row, not whether the row is good.  Re-deriving
    on top of either would duplicate exactly what the guard exists to
    prevent, and the janitor already owns the recovery of a stuck row.

    Indexed by ``idx_episodes_interaction`` on
    ``(agent_id, interaction_id)`` — migration 19, added by the PR B2
    review.  It first shipped riding ``idx_episodes_agent`` and filtering,
    on the reasoning that "this release ships two migrations already":
    that was wrong twice over.  The v0.3.15 scope lock's "two stores, two
    migrations" says the two stores are DISJOINT, not that the release
    may ship no more; and the checklist it cited is written at
    release-prep and did not exist.  Meanwhile the scan is linear in the
    agent's episode count, which has no ceiling for a persona, and the
    same predicate was already hot on the LIVE close path
    (``update_episode_summary``, once per speaker per room per close), so
    the index was never really this query's to pay for.  It is a covering
    index for this one: 15.8 ms → 0.002 ms at 20 000 episodes.
    """
    async with db.execute(
        "SELECT 1 FROM episodes WHERE agent_id = ? AND interaction_id = ? "
        "LIMIT 1",
        (agent_id, interaction_id),
    ) as cursor:
        return await cursor.fetchone() is not None
