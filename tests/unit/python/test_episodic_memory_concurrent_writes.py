"""Regression test for ISSUE-0055 — close-path SQLite commit race.

When a persona agent restarts, the channel catch-up replays a backlog of
stale events and the resulting burst of RFC 0020 idle-gap interaction
closes fans out many concurrent ``EpisodicMemory.store_episode`` /
``update_episode_summary`` calls onto one shared ``aiosqlite``
connection.  Each write runs its INSERT/UPDATE and its COMMIT as two
separate ``await``s; before the fix nothing serialised that pair, so one
close's COMMIT could land while another close's statement was still in
flight and SQLite raised::

    sqlite3.OperationalError: cannot commit transaction -
    SQL statements in progress

The affected episode never persisted and the janitor backfilled it to a
summary sentinel, degrading episodic recall for any interaction unlucky
enough to close during a catch-up storm.

These tests reproduce the race deterministically with a connection
wrapper that models exactly that SQLite failure mode — a ``commit()``
that runs while another coroutine is between its own ``execute()`` and
``commit()`` raises the real ``OperationalError``.  A writer that holds
its INSERT+COMMIT as one atomic critical section never trips the
wrapper; an interleaved one always does.  The fix is a per-store
``asyncio`` write lock around that critical section.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import aiosqlite

from agents.memory.episodic import EpisodicMemory
from agents.memory.interactions import SUMMARY_PENDING_TEXT

# Catch-up storms replay dozens of stale events (the ISSUE-0055 capture
# saw ~68); a handful of concurrent closes is already enough to expose
# the interleaving.
_CONCURRENT_CLOSES = 8


class _CommitRaceConnection:
    """``aiosqlite.Connection`` wrapper that reproduces the ISSUE-0055 race.

    Models SQLite's "cannot commit transaction - SQL statements in
    progress": a ``commit()`` raises when more than one coroutine is
    currently inside an ``execute()`` → ``commit()`` span on the shared
    connection.

    ``_in_flight`` is incremented at the *start* of ``execute()`` —
    before its first ``await`` — so every concurrently-scheduled writer
    has registered before any of them reaches ``commit()``.  That makes
    the interleaving deterministic: it does not depend on aiosqlite
    worker-thread timing.  A correctly-serialised writer holds a lock
    across the whole span, so only one writer is ever in flight and the
    counter never exceeds 1.

    Reads and writes that are not under test pass straight through via
    ``__getattr__``; only ``execute`` / ``commit`` are instrumented.
    """

    def __init__(self, real: aiosqlite.Connection) -> None:
        self._real = real
        self._in_flight = 0

    async def execute(self, *args: Any, **kwargs: Any) -> aiosqlite.Cursor:
        self._in_flight += 1
        return await self._real.execute(*args, **kwargs)

    async def commit(self) -> None:
        if self._in_flight > 1:
            raise sqlite3.OperationalError(
                "cannot commit transaction - SQL statements in progress",
            )
        await self._real.commit()
        self._in_flight -= 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class TestConcurrentClosePathWrites:
    """ISSUE-0055 — concurrent close-path writes must not race the shared
    SQLite connection's COMMIT."""

    async def test_concurrent_store_episode_calls_all_persist(
        self, memory: EpisodicMemory,
    ) -> None:
        """A catch-up storm of concurrent Phase-1 close-path INSERTs must
        all commit — every episode persists, none is lost to the race."""
        real_db = memory._ensure_db()
        memory._db = _CommitRaceConnection(real_db)  # type: ignore[assignment]
        try:
            results = await asyncio.gather(
                *(
                    memory.store_episode(
                        summary=SUMMARY_PENDING_TEXT,
                        context={"scope": f"dm:ember-owl:user-{i}"},
                        interaction_id=f"iid-{i}",
                        started_at=100.0 + i,
                        closed_at=110.0 + i,
                        turn_count=2,
                        scope=f"dm:ember-owl:user-{i}",
                    )
                    for i in range(_CONCURRENT_CLOSES)
                ),
                return_exceptions=True,
            )
        finally:
            memory._db = real_db

        raced = [r for r in results if isinstance(r, BaseException)]
        assert not raced, (
            f"concurrent close-path INSERTs raced the shared connection: {raced}"
        )
        # Every close persisted its own row — no episode lost to the race.
        async with real_db.execute(
            "SELECT interaction_id FROM episodes WHERE agent_id = ?",
            (memory.agent_id,),
        ) as cursor:
            persisted = {row[0] for row in await cursor.fetchall()}
        assert persisted == {f"iid-{i}" for i in range(_CONCURRENT_CLOSES)}

    async def test_concurrent_update_episode_summary_calls_all_commit(
        self, memory: EpisodicMemory,
    ) -> None:
        """The Phase-2 close-path UPDATE shares the same hazard: a burst of
        background summary commits must not race each other's statements."""
        # Phase-1 rows, written serially before the race wrapper is in
        # place so the concurrency under test is purely the UPDATE path.
        for i in range(_CONCURRENT_CLOSES):
            await memory.store_episode(
                summary=SUMMARY_PENDING_TEXT,
                context={"scope": f"dm:ember-owl:user-{i}"},
                interaction_id=f"iid-{i}",
                started_at=100.0 + i,
                closed_at=110.0 + i,
                turn_count=2,
                scope=f"dm:ember-owl:user-{i}",
            )

        real_db = memory._ensure_db()
        memory._db = _CommitRaceConnection(real_db)  # type: ignore[assignment]
        try:
            results = await asyncio.gather(
                *(
                    memory.update_episode_summary(f"iid-{i}", f"real summary {i}")
                    for i in range(_CONCURRENT_CLOSES)
                ),
                return_exceptions=True,
            )
        finally:
            memory._db = real_db

        raced = [r for r in results if isinstance(r, BaseException)]
        assert not raced, (
            f"concurrent Phase-2 UPDATEs raced the shared connection: {raced}"
        )
        # Every UPDATE replaced its pending sentinel — no summary lost.
        assert all(updated is True for updated in results)
