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

from .interaction_janitor import SUMMARY_UNAVAILABLE_TEXT

__all__ = [
    "clear_failed_episode_for_interaction",
    "episode_exists_for_interaction",
]


async def episode_exists_for_interaction(
    db: aiosqlite.Connection, agent_id: str, interaction_id: str,
) -> bool:
    """``True`` iff ``agent_id`` already DERIVED an episode for this span.

    A row the janitor finalised to ``[interaction summary unavailable]``
    does NOT count, and that exclusion is the whole correction (PR B2
    review).  Counting it made a transient Phase-2 failure permanent: the
    digest is boot-stable, so every later boot recomputed the same id,
    matched the tombstone, and declined — while
    ``cleanup_closing_interactions`` only rewrites the sentinel and
    ``update_episode_summary`` matches ``[summary pending]`` alone, so
    nothing ever retried the summary.  The janitor owns the recovery of a
    stuck ROW; it does not recover the span's CONTENT, and the turns only
    ever lived in memory (``close_path._TRANSIENT_TURN_KEYS`` keeps bodies
    out of ``context_json``).  Losing a span's memory to one provider
    hiccup on the boot path inverts the cost rule
    :mod:`agents.persona_runtime.replay_identity` applies everywhere else.

    A ``[summary pending]`` row DOES still count.  It means either a
    Phase 2 in flight or a boot that died before one, and the two are
    indistinguishable from here — so the guard waits for the janitor to
    convert it (``DEFAULT_CLOSING_GRACE_SEC``), which costs one extra boot
    and cannot race a live writer.  Recovery is therefore bounded and
    automatic rather than permanent.

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
        "AND summary <> ? LIMIT 1",
        (agent_id, interaction_id, SUMMARY_UNAVAILABLE_TEXT),
    ) as cursor:
        return await cursor.fetchone() is not None


async def clear_failed_episode_for_interaction(
    db: aiosqlite.Connection, agent_id: str, interaction_id: str,
) -> int:
    """Drop the tombstone rows blocking a re-derivation of this span.

    Called only once :func:`episode_exists_for_interaction` has answered
    ``False``, i.e. immediately before the caller re-derives.  Without it
    the retry would leave one ``[interaction summary unavailable]`` row
    per failed boot under a single digest — an unbounded growth curve
    under the guard that exists to bound one, and a set of rows recall can
    only ever return as noise.

    Deleting is safe precisely because the sentinel is TERMINAL:
    ``cleanup_closing_interactions`` writes it only after
    ``DEFAULT_CLOSING_GRACE_SEC``, and ``update_episode_summary`` refuses
    to overwrite it, so no writer can still be holding this row.  It also
    keeps ``interaction_id`` effectively single-rowed, which
    ``update_episode_summary`` depends on — it matches without a ``LIMIT``
    and reports ``rowcount``.
    """
    cursor = await db.execute(
        "DELETE FROM episodes WHERE agent_id = ? AND interaction_id = ? "
        "AND summary = ?",
        (agent_id, interaction_id, SUMMARY_UNAVAILABLE_TEXT),
    )
    deleted = cursor.rowcount or 0
    if deleted:
        await db.commit()
    return deleted
