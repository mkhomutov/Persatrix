"""Regression test for ISSUE-0061 — the RelationshipMemory.update_trust
RETURNING commit race.

``RelationshipMemory`` shares a single ``aiosqlite`` connection across an
agent's async tasks — the same single-connection design ``EpisodicMemory``
uses.  ``update_trust`` issues an ``INSERT … ON CONFLICT DO UPDATE …
RETURNING trust_score`` upsert, and ``aiosqlite.Connection.execute()``
steps a ``RETURNING`` statement only to its first result row.  The
*write* VDBE therefore stays active across the ``await`` gap between
``execute()`` and the ``fetchone()`` that drains it.

SQLite raises::

    sqlite3.OperationalError: cannot commit transaction -
    SQL statements in progress

whenever a ``COMMIT`` runs while another *write* statement is still an
active VDBE (``db->nVdbeWrite > 0``).  So any plain-DML writer that
``COMMIT``s on the shared connection while ``update_trust`` sits in that
``execute()`` → ``fetchone()`` gap raises — and either the trust update
or the innocent concurrent writer fails.

This is the same defect ISSUE-0055 fixed for ``increment_interaction_count``
on the episodic connection.  ``update_trust`` is its structural twin on
the relationship connection: ``increment_interaction_count`` genuinely
was the sole ``RETURNING`` writer *there*, but the relationship tier has
its own ``RETURNING`` writer that the ISSUE-0055 fix did not reach.

The race is currently latent — no production caller drives
``update_trust`` concurrently with another writer on its connection
today.  This test constructs that concurrency on purpose so the fix is
locked in before a future caller (e.g. a periodic ``apply_decay``
background sweep running alongside interaction-driven trust updates)
silently re-arms it.

``apply_decay`` is the racing companion here precisely because it is a
plain two-statement writer (``UPDATE`` + ``COMMIT``): listed ahead of
the ``RETURNING`` writer, its ``COMMIT`` is queued into ``update_trust``'s
suspended-VDBE window.  The test drives a real ``RelationshipMemory`` on
a real ``aiosqlite`` connection — SQLite raises the real error, nothing
is simulated.  The fix drains the ``RETURNING`` row in a single
``execute_fetchall`` round-trip so the write VDBE is never suspended
across an ``await``.
"""

from __future__ import annotations

import asyncio

import pytest
from agents.memory.relationship import RelationshipMemory

# A decay sweep racing a burst of trust updates reproduces the race far
# below any realistic scale — a handful of each writer trips it on every
# run.
_CONCURRENT_WRITERS = 8


@pytest.fixture
async def memory():
    """An initialized RelationshipMemory on an in-memory DB."""
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestUpdateTrustReturningCommitRace:
    """ISSUE-0061 — a plain-DML ``COMMIT`` must not break a concurrent
    ``update_trust`` ``INSERT … RETURNING`` on the shared relationship
    connection."""

    async def test_decay_sweep_does_not_race_update_trust_returning(
        self, memory: RelationshipMemory,
    ) -> None:
        """A decay sweep and a burst of trust updates run concurrently on
        one connection.

        Every write must complete: no ``update_trust`` upsert and no
        ``apply_decay`` sweep lost to the ``RETURNING`` commit race.
        """
        n = _CONCURRENT_WRITERS

        # Seed peers at non-neutral trust so every concurrent apply_decay
        # has rows to update and therefore COMMITs — making it the
        # two-statement plain writer whose COMMIT races the RETURNING gap.
        for i in range(n):
            await memory.update_trust(f"peer-{i}", delta=0.2, reason=f"seed-{i}")

        # apply_decay first, update_trust second: the plain writer's
        # COMMIT is then queued ahead of the RETURNING writer's drain, so
        # the COMMIT lands while the RETURNING write VDBE is still active.
        decays = [
            memory.apply_decay(decay_rate=0.05) for _ in range(n)
        ]
        trust_updates = [
            memory.update_trust(f"peer-{i}", delta=0.05, reason=f"storm-{i}")
            for i in range(n)
        ]
        # gather preserves order: decays, then trust_updates.
        results = await asyncio.gather(
            *decays, *trust_updates, return_exceptions=True,
        )

        raced = [r for r in results if isinstance(r, BaseException)]
        assert not raced, (
            f"a decay sweep raced update_trust's RETURNING COMMIT on the "
            f"shared relationship connection: {raced}"
        )

        decay_results = results[:n]
        trust_results = results[n:]

        # Every decay sweep found all seeded rows and committed — so each
        # one really was the plain writer providing a racing COMMIT.
        assert all(updated == n for updated in decay_results)

        # Every trust update returned a clamped, finite score; no upsert
        # was lost to a failed COMMIT.
        assert all(
            isinstance(t, float) and 0.0 <= t <= 1.0 for t in trust_results
        )

        # Every peer row persisted with a valid trust score.
        for i in range(n):
            stored = await memory.get_trust(f"peer-{i}")
            assert 0.0 <= stored <= 1.0
