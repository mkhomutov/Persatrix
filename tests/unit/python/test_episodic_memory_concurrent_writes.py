"""Regression test for ISSUE-0055 — the close-path SQLite commit race.

When a persona agent restarts, channel catch-up replays a backlog of
stale events; the burst of RFC 0020 idle-gap interaction closes fans out
many concurrent writes onto the one ``aiosqlite`` connection
``EpisodicMemory`` shares across the agent's async tasks.  One of those
writes then fails to ``COMMIT`` with::

    sqlite3.OperationalError: cannot commit transaction -
    SQL statements in progress

the affected episode never persists, and the janitor backfills the row
to a summary sentinel — degrading episodic recall.

Mechanism
---------
SQLite raises that error when a ``COMMIT`` runs while another *write*
statement is still an active VDBE (``db->nVdbeWrite > 0``).  A plain
``INSERT`` / ``UPDATE`` / ``DELETE`` is stepped to completion inside its
own ``execute()`` and leaves no active VDBE — so concurrent plain writes
(``store_episode``, ``update_episode_summary``) never trip it, even
though they share the connection.

The one statement that *does* leave a write VDBE suspended is an
``INSERT … RETURNING``: ``aiosqlite.Connection.execute()`` steps it only
to its first result row, so the write VDBE stays active across the
``await`` gap between ``execute()`` and the ``fetchone()`` that drains
it.  ``increment_interaction_count`` is the sole ``RETURNING`` writer on
the episodic connection.  In the persona runtime it runs from the
Phase-2 background task (``_tick_auto_reflect_counter``, outside the
agent lock); during a catch-up storm its suspended ``RETURNING`` VDBE
overlaps a close-path ``store_episode`` ``COMMIT``, which then raises.

This test reproduces the race against a real ``EpisodicMemory`` and a
real ``aiosqlite`` connection — the SQLite library raises the real
error, nothing is simulated.  The fix drains the ``RETURNING`` cursor in
a single round-trip so the write VDBE is never suspended across an
``await``.
"""

from __future__ import annotations

import asyncio

from agents.memory.episodic import EpisodicMemory
from agents.memory.interactions import SUMMARY_PENDING_TEXT

# Catch-up storms replay dozens of stale events (the ISSUE-0055 capture
# saw ~68).  The race is deterministic far below that: a handful of
# concurrent counter increments and close-path writes reproduces it on
# every run.
_CONCURRENT_CLOSES = 8


class TestClosePathCommitRace:
    """ISSUE-0055 — a Phase-2 counter increment must not break a
    concurrent close-path ``COMMIT`` on the shared episodic connection."""

    async def test_catchup_storm_does_not_race_the_shared_commit(
        self, memory: EpisodicMemory,
    ) -> None:
        """A catch-up storm interleaves the three close-path writers on
        one connection — Phase-1 ``store_episode`` INSERTs, Phase-2
        ``update_episode_summary`` UPDATEs, and the Phase-2
        ``increment_interaction_count`` ``INSERT … RETURNING``.

        Every write must commit: no episode lost to the ``RETURNING``
        commit race, every pending summary replaced, and the counter
        increments serialised cleanly with no lost update.
        """
        # Phase-2 ``update_episode_summary`` needs existing pending rows.
        # Create them serially, before the storm, so the only concurrency
        # under test is the storm itself.
        for i in range(_CONCURRENT_CLOSES):
            await memory.store_episode(
                summary=SUMMARY_PENDING_TEXT,
                context={"scope": f"dm:ember-owl:seed-{i}"},
                interaction_id=f"seed-iid-{i}",
                started_at=100.0 + i, closed_at=110.0 + i,
                turn_count=2, scope=f"dm:ember-owl:seed-{i}",
            )

        new_closes = [
            memory.store_episode(
                summary=SUMMARY_PENDING_TEXT,
                context={"scope": f"dm:ember-owl:new-{i}"},
                interaction_id=f"new-iid-{i}",
                started_at=200.0 + i, closed_at=210.0 + i,
                turn_count=2, scope=f"dm:ember-owl:new-{i}",
            )
            for i in range(_CONCURRENT_CLOSES)
        ]
        summaries = [
            memory.update_episode_summary(f"seed-iid-{i}", f"real summary {i}")
            for i in range(_CONCURRENT_CLOSES)
        ]
        increments = [
            memory.increment_interaction_count()
            for _ in range(_CONCURRENT_CLOSES)
        ]
        # gather preserves order: new_closes, then summaries, then
        # increments.
        results = await asyncio.gather(
            *new_closes, *summaries, *increments, return_exceptions=True,
        )

        raced = [r for r in results if isinstance(r, BaseException)]
        assert not raced, (
            f"a catch-up storm raced the shared episodic connection's "
            f"COMMIT: {raced}"
        )

        n = _CONCURRENT_CLOSES
        summary_results = results[n:2 * n]
        increment_results = results[2 * n:]

        # Every fresh close persisted its own row; the seed rows survived.
        db = memory._ensure_db()
        async with db.execute(
            "SELECT interaction_id FROM episodes WHERE agent_id = ?",
            (memory.agent_id,),
        ) as cursor:
            persisted = {row[0] for row in await cursor.fetchall()}
        assert persisted == (
            {f"seed-iid-{i}" for i in range(n)}
            | {f"new-iid-{i}" for i in range(n)}
        )

        # Every background summary replaced its pending sentinel.
        assert all(updated is True for updated in summary_results)

        # The RETURNING upserts serialised on the connection's worker
        # thread, so the post-increment counts are exactly 1..n with no
        # lost update.
        assert sorted(increment_results) == list(range(1, n + 1))
