"""Every memory write ends its transaction (ISSUE-0197).

A persona's memory tiers each hold their own connection to one
``memory.db``, and SQLite lets only one connection write at a time.
Python's driver opens a transaction before every ``INSERT``, ``UPDATE``
and ``DELETE``, whether or not the statement goes on to change a row, and
keeps it open until that connection commits.  A write that returns without
committing therefore keeps the write lock, and every other tier's writes
fail with ``database is locked`` until the connection's next commit.

So a write in this package goes through one of two helpers, and both end
the transaction however the write ends:

* :func:`execute_write` for a write that is one statement;
* :func:`write_boundary` around a write that is several, or
  :func:`ends_transaction` on a function that is one such write.

Both end it with a commit, on the error path too, and never with a
rollback.  A tier's connection is shared by every coroutine of its
persona with no lock around it (ISSUE-0055 and ISSUE-0061 are that
sharing).  A statement another coroutine has run and not yet committed
sits in the same transaction, and a rollback here would discard it: its
own commit would then succeed and save nothing.  That is easiest to hit
exactly when the lock is contended, because a statement waiting for the
lock holds up the connection's queue and other coroutines' statements
line up behind it.  A commit discards nothing.  What it can keep is the
first half of a write that failed part-way, which is what happens today
at the connection's next commit, only sooner (ISSUE-0237).

A statement that raised changed nothing itself (SQLite undoes a failed
statement), so for a one-statement write the commit only ends the
transaction.

The migration runner and the timer cache roll back instead: each is the
only user of its connection while it runs.

``tests/unit/python/test_memory_write_boundary.py`` reads this package
and fails when a write statement is outside both helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, Concatenate, ParamSpec, TypeVar

import aiosqlite

__all__ = ["ends_transaction", "execute_write", "write_boundary"]

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


@contextlib.asynccontextmanager
async def write_boundary(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """Run a write of one or more statements and end its transaction.

    Commits when the block returns.  When the block raises, or its task
    is cancelled, the transaction is still ended, by a commit (see the
    module docstring for why not a rollback), and the error is raised
    again unchanged.  A block that has already committed is committed
    again, which does nothing.
    """
    try:
        yield
    except (Exception, asyncio.CancelledError):
        # No look at ``in_transaction`` first: a cancelled task's
        # statement may still be waiting in the connection's queue, and
        # this commit has to run after it.  The commit is queued before
        # the await below can be interrupted, so a second cancellation
        # does not stop it.  It waits for that statement, which under
        # contention can take as long as the connection's busy timeout.
        try:
            await db.commit()
        except Exception:
            logger.warning(
                "A memory write failed and its transaction could not be "
                "ended; the connection keeps the write lock until its "
                "next commit",
                exc_info=True,
            )
        raise
    await db.commit()


async def execute_write(
    db: aiosqlite.Connection,
    sql: str,
    parameters: Iterable[Any] | None = None,
) -> aiosqlite.Cursor:
    """Run one write statement as a whole write, and end its transaction.

    Returns the statement's cursor, for its ``rowcount``.  The commit
    does not depend on that count: a statement that changes no row has
    still opened a transaction.
    """
    async with write_boundary(db):
        return await db.execute(sql, parameters)


def ends_transaction(
    write: Callable[Concatenate[aiosqlite.Connection, _P], Awaitable[_R]],
) -> Callable[Concatenate[aiosqlite.Connection, _P], Awaitable[_R]]:
    """Decorate a write that takes the connection as its first argument.

    The whole call runs inside :func:`write_boundary`.  The function
    keeps its own commit where it has one, so anything it does after
    committing (a metric, an audit record) still follows the commit; the
    boundary's commit on the way out then finds nothing to do.

    Only for the function that owns the transaction.  A helper that
    writes on behalf of a caller, inside the caller's transaction, must
    not carry it: it would commit the caller's write half-way.
    """

    @functools.wraps(write)
    async def wrapper(
        db: aiosqlite.Connection, /, *args: _P.args, **kwargs: _P.kwargs,
    ) -> _R:
        async with write_boundary(db):
            return await write(db, *args, **kwargs)

    return wrapper
