"""A memory write gives the database's write lock back (ISSUE-0197).

A persona's memory tiers each hold their own connection to one
``memory.db``.  SQLite lets one connection write at a time, and Python's
driver opens a transaction before every ``INSERT``, ``UPDATE`` and
``DELETE``, so a write that never commits keeps every other tier out
until that connection's next commit.

Each test opens the episodic and the relationship tier on one file, runs
a write on one of them, and then writes on the other.  The second write
waits 50 ms for the lock here, not the driver's five seconds, so a held
lock fails the test at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest

from agents.memory._episodic_replay_dedup import (
    clear_failed_episode_for_interaction,
)
from agents.memory.episodic import EpisodicMemory
from agents.memory.episodic_retention import (
    delete_old_episodes,
    summarize_old_episodes,
)
from agents.memory.interaction_janitor import cleanup_closing_interactions
from agents.memory.migrations import _MIGRATION_HANDLERS, MIGRATIONS, _apply_migrations
from agents.memory.relationship import RelationshipMemory
from agents.memory.relationship_decay import apply_decay
from agents.memory.relationship_mutations import record_interaction
from agents.memory.scheduled_wakes import ScheduledWakeRow, ScheduledWakesCache

AGENT = "ember-owl"

#: How long the second tier waits for the lock.  Long enough for a
#: loaded CI runner to finish a commit, short enough to keep a held
#: lock from costing the suite the driver's five-second default.
BUSY_MS = 50

Tiers = tuple[EpisodicMemory, RelationshipMemory]


@pytest.fixture
async def tiers(tmp_path: Path) -> AsyncIterator[Tiers]:
    path = str(tmp_path / "memory.db")
    episodic = EpisodicMemory(agent_id=AGENT, db_path=path)
    relationship = RelationshipMemory(agent_id=AGENT, db_path=path)
    await episodic.initialize()
    await relationship.initialize()
    for db in (episodic._ensure_db(), relationship._ensure_db()):
        await db.execute(f"PRAGMA busy_timeout={BUSY_MS}")
    yield episodic, relationship
    await episodic.close()
    await relationship.close()


async def _assert_lock_released(
    wrote_on: aiosqlite.Connection, tiers: Tiers,
) -> None:
    """The connection that wrote holds no transaction, and the other tier
    can write straight away."""
    episodic, relationship = tiers
    assert not wrote_on.in_transaction, (
        "the write left its transaction open, so its connection still "
        "holds the write lock"
    )
    if wrote_on is episodic._ensure_db():
        await relationship.record_interaction("peer", "chat")
    else:
        await episodic.store_episode(summary="after the sweep", context={})


# ─── The five writes that committed only when a row changed ──────────


async def _janitor(tiers: Tiers) -> aiosqlite.Connection:
    db = tiers[0]._ensure_db()
    assert await cleanup_closing_interactions(db, AGENT) == 0
    return db


async def _clear_failed(tiers: Tiers) -> aiosqlite.Connection:
    db = tiers[0]._ensure_db()
    assert await clear_failed_episode_for_interaction(db, AGENT, "no-such") == 0
    return db


async def _delete_old(tiers: Tiers) -> aiosqlite.Connection:
    db = tiers[0]._ensure_db()
    assert await delete_old_episodes(db, AGENT, 90.0) == 0
    return db


async def _summarize_old(tiers: Tiers) -> aiosqlite.Connection:
    """The update matches no row when the episode goes away while the
    model is writing its summary."""
    episodic = tiers[0]
    db = episodic._ensure_db()
    episode_id = await episodic.store_episode(summary="raw", context={})
    await db.execute(
        "UPDATE episodes SET created_at = ? WHERE id = ?",
        (time.time() - 100 * 86400, episode_id),
    )
    await db.commit()

    class _Model:
        async def create_message(self, **_: Any) -> SimpleNamespace:
            await db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
            await db.commit()
            return SimpleNamespace(text="a summary", usage=None)

    resolved = SimpleNamespace(model="m", alias="summarizer")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "agents.memory.episodic_retention.resolve", lambda _alias: resolved,
        )
        assert await summarize_old_episodes(db, AGENT, 90.0, _Model()) == 0  # type: ignore[arg-type]
    return db


async def _decay(tiers: Tiers) -> aiosqlite.Connection:
    db = tiers[1]._ensure_db()
    assert await apply_decay(db, AGENT) == 0
    return db


@pytest.mark.parametrize(
    "zero_row_write",
    [_janitor, _clear_failed, _delete_old, _summarize_old, _decay],
    ids=[
        "cleanup_closing_interactions",
        "clear_failed_episode_for_interaction",
        "delete_old_episodes",
        "summarize_old_episodes",
        "apply_decay",
    ],
)
async def test_a_write_that_changes_no_row_releases_the_lock(
    tiers: Tiers,
    zero_row_write: Callable[[Tiers], Awaitable[aiosqlite.Connection]],
) -> None:
    wrote_on = await zero_row_write(tiers)
    await _assert_lock_released(wrote_on, tiers)


# ─── A write that raises, or whose task is cancelled ─────────────────


class _InterruptedAfter:
    """A connection whose Nth write statement runs and then raises.

    This is what a cancelled task looks like from inside a write: the
    statement has reached SQLite, and the line that would have committed
    it never runs.  Everything else goes to the real connection.
    """

    def __init__(
        self, real: aiosqlite.Connection, *, nth: int = 1,
        error: BaseException | None = None,
    ) -> None:
        self._real = real
        self._left = nth
        self._error = error if error is not None else asyncio.CancelledError()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def execute(self, sql: str, parameters: Any = None) -> Any:
        cursor = await self._real.execute(sql, parameters or ())
        if sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            self._left -= 1
            if self._left == 0:
                raise self._error
        return cursor


async def test_a_cancelled_single_statement_write_releases_the_lock(
    tiers: Tiers,
) -> None:
    db = tiers[0]._ensure_db()
    with pytest.raises(asyncio.CancelledError):
        await cleanup_closing_interactions(_InterruptedAfter(db), AGENT)  # type: ignore[arg-type]
    await _assert_lock_released(db, tiers)


# A real cancellation differs from the double above in one way that
# matters: the task stops waiting at once, while its statement may still
# be in the connection's queue and run a moment later.


async def _cancel_for_real(write: Awaitable[Any], *, after_turns: int) -> None:
    task = asyncio.ensure_future(write)
    for _ in range(after_turns):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("after_turns", range(8))
@pytest.mark.parametrize("write", ["one statement", "two statements"])
async def test_a_write_cancelled_at_any_point_releases_the_lock(
    tiers: Tiers, write: str, after_turns: int,
) -> None:
    """``task.cancel()`` on a running write, after each number of turns
    of the event loop from none (the write never starts) to more than
    either write needs to finish."""
    if write == "one statement":
        db = tiers[0]._ensure_db()
        running: Awaitable[Any] = cleanup_closing_interactions(db, AGENT)
    else:
        db = tiers[1]._ensure_db()
        running = record_interaction(db, AGENT, "peer", "chat")
    await _cancel_for_real(running, after_turns=after_turns)
    await _assert_lock_released(db, tiers)


async def test_a_write_cancelled_while_its_statement_is_still_queued_releases_the_lock(
    tiers: Tiers,
) -> None:
    """The case a look at ``in_transaction`` gets wrong.  The connection
    is busy with an earlier query when the write is cancelled, so the
    write's statement has not run yet and no transaction is open.  The
    statement runs afterwards and opens one; the boundary's commit has
    to be queued behind it."""
    db = tiers[0]._ensure_db()
    await db.create_function("pause", 1, time.sleep)
    earlier = asyncio.ensure_future(db.execute("SELECT pause(0.1)"))
    await asyncio.sleep(0)
    task = asyncio.ensure_future(cleanup_closing_interactions(db, AGENT))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not db.in_transaction, "the write's statement ran before the cancellation"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await (await earlier).close()
    await (await db.execute("SELECT 1")).close()  # the queue has drained
    await _assert_lock_released(db, tiers)


async def test_a_write_that_fails_part_way_releases_the_lock(
    tiers: Tiers,
) -> None:
    """``record_interaction`` runs two statements.  When the second fails
    the first is kept (ISSUE-0237): rolling back could discard a statement
    another coroutine has pending on this same connection (see the
    boundary's own tests).  Before the boundary the first was kept too
    once the connection committed again, and lost if it closed first."""
    db = tiers[1]._ensure_db()
    broken = _InterruptedAfter(db, nth=2, error=sqlite3.OperationalError("boom"))
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        await record_interaction(broken, AGENT, "peer", "chat")  # type: ignore[arg-type]
    await _assert_lock_released(db, tiers)
    async with tiers[0]._ensure_db().execute(
        "SELECT COUNT(*) FROM interactions",
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 1, "the first statement's row is kept"


async def test_a_failed_migration_is_rolled_back_and_releases_the_lock(
    tiers: Tiers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration that fails half-way must not leave half its rows
    waiting to be committed, nor hold the lock on a database whose boot
    has just failed."""
    db = tiers[0]._ensure_db()
    version = MIGRATIONS[-1][0] + 1

    async def _half_done(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO agent_state (agent_id, interaction_count, updated_at) "
            "VALUES ('half-migrated', 7, 0)",
        )
        raise RuntimeError("migration failed")

    monkeypatch.setattr(
        "agents.memory.migrations.MIGRATIONS",
        [*MIGRATIONS, (version, "fails half-way", "")],
    )
    monkeypatch.setitem(_MIGRATION_HANDLERS, version, _half_done)

    with pytest.raises(RuntimeError, match="migration failed"):
        await _apply_migrations(db)
    await _assert_lock_released(db, tiers)
    async with db.execute(
        "SELECT COUNT(*) FROM agent_state WHERE agent_id = 'half-migrated'",
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0


async def test_a_cancelled_timer_cache_rebuild_releases_the_lock(
    tiers: Tiers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timer cache rolled back on an error but not on a cancellation,
    which is not an ``Exception``."""
    cache = ScheduledWakesCache(db_path=str(tmp_path / "memory.db"), agent_id=AGENT)
    await cache.initialize()
    conn = cache._conn
    assert conn is not None
    await conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")

    async def _cancelled() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(conn, "commit", _cancelled)
    row = ScheduledWakeRow(
        timer_id="t", kind="interval", interval_ms=1000, jitter_ms=0,
        next_fire_at_ms=0, source="config",
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await cache.rebuild_from_config([row])
        assert not conn.in_transaction
        await tiers[1].record_interaction("peer", "chat")
    finally:
        await cache.close()
