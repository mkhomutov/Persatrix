"""The helpers every memory write goes through, and the check that every
memory write does (ISSUE-0197).

Two halves:

* what :mod:`agents.memory._write_boundary` does when a write changes no
  row, raises, or is cancelled;
* a reading of ``agents/memory/`` and ``agents/participant.py`` that fails
  when a write statement sits outside those helpers, so the next write added to the package cannot
  leave a transaction open by forgetting a commit.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

import agents.memory
from agents.memory._write_boundary import (
    ends_transaction,
    execute_write,
    write_boundary,
)

BUSY_MS = 50


@pytest.fixture
async def pair(tmp_path: Path) -> AsyncIterator[tuple[aiosqlite.Connection, aiosqlite.Connection]]:
    """Two connections to one database, as two memory tiers hold."""
    path = str(tmp_path / "memory.db")
    first = await aiosqlite.connect(path)
    second = await aiosqlite.connect(path)
    await first.execute("PRAGMA journal_mode=WAL")
    await first.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    await first.commit()
    for db in (first, second):
        await db.execute(f"PRAGMA busy_timeout={BUSY_MS}")
    yield first, second
    await first.close()
    await second.close()


async def _ids(db: aiosqlite.Connection) -> list[int]:
    async with db.execute("SELECT id FROM t ORDER BY id") as cursor:
        return [row[0] for row in await cursor.fetchall()]


class _Spy:
    """Counts commits and can make a statement raise after it has run."""

    def __init__(
        self, real: aiosqlite.Connection, *,
        raise_after_execute: BaseException | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self._real = real
        self._raise = raise_after_execute
        self._commit_error = commit_error
        self.commits = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def execute(self, sql: str, parameters: Any = None) -> Any:
        cursor = await self._real.execute(sql, parameters or ())
        if self._raise is not None:
            raise self._raise
        return cursor

    async def commit(self) -> None:
        self.commits += 1
        if self._commit_error is not None:
            raise self._commit_error
        await self._real.commit()


# ─── What the helpers do ─────────────────────────────────────────────


async def test_a_statement_that_changes_no_row_is_committed(pair: Any) -> None:
    first, second = pair
    cursor = await execute_write(first, "UPDATE t SET v = 'x' WHERE id = 99")
    assert cursor.rowcount == 0
    assert not first.in_transaction
    await second.execute("INSERT INTO t (id) VALUES (1)")  # not locked out
    await second.commit()


async def test_a_statement_that_raises_ends_its_transaction(pair: Any) -> None:
    first, second = pair
    await execute_write(first, "INSERT INTO t (id) VALUES (1)")
    with pytest.raises(sqlite3.IntegrityError):
        await execute_write(first, "INSERT INTO t (id) VALUES (1)")
    assert not first.in_transaction
    await second.execute("INSERT INTO t (id) VALUES (2)")
    await second.commit()


async def test_a_cancelled_statement_is_committed_after_it_runs(pair: Any) -> None:
    """The statement reached SQLite before the cancellation, so it is
    kept: that is what the connection's next commit does to it today."""
    first, second = pair
    spy = _Spy(first, raise_after_execute=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await execute_write(spy, "INSERT INTO t (id) VALUES (1)")  # type: ignore[arg-type]
    assert not first.in_transaction
    assert await _ids(second) == [1]


async def test_a_failed_write_keeps_another_coroutines_pending_statement(
    pair: Any,
) -> None:
    """Why the error path commits and does not roll back.

    A tier's connection is shared by all of its persona's coroutines.
    Here one has run its statement and not yet committed when another's
    write fails.  A rollback would discard the first one's row, and its
    own commit would then save nothing.
    """
    shared, other_tier = pair
    await execute_write(shared, "INSERT INTO t (id) VALUES (5)")

    await shared.execute("INSERT INTO t (id) VALUES (1)")  # coroutine A, not yet committed
    with pytest.raises(sqlite3.IntegrityError):  # coroutine B's write fails
        await execute_write(shared, "INSERT INTO t (id) VALUES (5)")
    await shared.commit()  # coroutine A commits

    assert await _ids(other_tier) == [1, 5]


async def test_a_block_that_raises_reports_the_blocks_error_not_the_commits(
    pair: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    first, _ = pair
    spy = _Spy(first, commit_error=sqlite3.OperationalError("commit failed"))
    with caplog.at_level(logging.WARNING, logger="agents.memory._write_boundary"):
        with pytest.raises(ValueError, match="the write's own error"):
            async with write_boundary(spy):  # type: ignore[arg-type]
                raise ValueError("the write's own error")
    assert "could not be ended" in caplog.text


async def test_a_block_of_statements_is_one_commit(pair: Any) -> None:
    first, second = pair
    spy = _Spy(first)
    async with write_boundary(spy):  # type: ignore[arg-type]
        await spy.execute("INSERT INTO t (id) VALUES (1)")
        assert await _ids(second) == [], "nothing is committed part-way"
        await spy.execute("INSERT INTO t (id) VALUES (2)")
    assert spy.commits == 1 and not first.in_transaction
    assert await _ids(second) == [1, 2]


async def test_the_decorator_ends_the_transaction_on_both_paths(pair: Any) -> None:
    first, second = pair

    @ends_transaction
    async def write(db: aiosqlite.Connection, row_id: int, *, fail: bool = False) -> int:
        await db.execute("INSERT INTO t (id) VALUES (?)", (row_id,))
        if fail:
            raise RuntimeError("after the statement")
        return row_id

    assert write.__name__ == "write"
    assert await write(first, 1) == 1
    assert not first.in_transaction
    with pytest.raises(RuntimeError, match="after the statement"):
        await write(first, 2, fail=True)
    assert not first.in_transaction
    assert await _ids(second) == [1, 2]


# ─── Every write in the package goes through them ────────────────────

_MEMORY = Path(agents.memory.__file__).parent
_EXECUTORS = {
    "execute", "executemany", "executescript", "execute_fetchall", "execute_insert",
}
_WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE"}

#: Writes that run inside a transaction their caller owns and ends, or
#: that end their own another way.  Each entry is a reviewed decision.
_ENDED_ELSEWHERE = {
    "_facts_supersede.py::apply_supersession":
        "part of insert_fact's write, which carries @ends_transaction",
    "notes.py::_prune_notes":
        "part of store_note's write, inside its write_boundary",
    "migrations.py::_apply_pending_migrations":
        "_apply_migrations rolls back when it raises",
    "scheduled_wakes.py::rebuild_from_config":
        "the timer cache's own connection; rolls back on error or cancellation",
}

#: Statements whose text this check cannot read, each confirmed a read.
_NOT_A_WRITE = {
    "relationship_queries.py::get_trust": "a SELECT built by _pair_row_query",
    "relationship_queries.py::get_identity": "a SELECT built by _pair_row_query",
    "relationship_queries.py::get_relationship_summary":
        "a SELECT built by _pair_row_query",
    "episodic.py::initialize":
        "table and trigger definitions; executescript commits before it "
        "runs and each statement commits itself",
}


def _literal_text(node: ast.AST, scopes: list[ast.AST], depth: int = 0) -> str:
    """The string literals a SQL argument is built from, in order."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(part, scopes, depth) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left, scopes, depth) + _literal_text(
            node.right, scopes, depth,
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _literal_text(node.func.value, scopes, depth)
    if isinstance(node, ast.Name) and depth < 4:
        for scope in scopes:
            for stmt in ast.walk(scope):
                targets: list[ast.expr] = []
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
                    targets = [stmt.target]
                if stmt_value := getattr(stmt, "value", None):
                    if any(isinstance(t, ast.Name) and t.id == node.id for t in targets):
                        text = _literal_text(stmt_value, scopes, depth + 1)
                        if text.strip():
                            return text
    return ""


def _kind(sql: str) -> str:
    words = sql.split()
    if not words:
        return "unknown"
    verb = words[0].upper()
    if verb in _WRITE_VERBS:
        return "write"
    if verb == "WITH" and _WRITE_VERBS & {w.upper() for w in words}:
        return "write"
    return "other"


def _is_helper(node: ast.AST, name: str) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id == name) or (
        isinstance(target, ast.Attribute) and target.attr == name
    )


def _unguarded(source: str) -> Iterator[tuple[str, int, str]]:
    """``(function, line, kind)`` for each statement run outside the helpers
    that is a write, or whose text cannot be read."""
    tree = ast.parse(source)

    def visit(node: ast.AST, function: ast.AST | None, guarded: bool) -> Iterator[Any]:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node
            guarded = any(_is_helper(d, "ends_transaction") for d in node.decorator_list)
        elif isinstance(node, ast.AsyncWith):
            guarded = guarded or any(
                _is_helper(item.context_expr, "write_boundary") for item in node.items
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _EXECUTORS
            and node.args
            and not guarded
        ):
            scopes = [s for s in (function, tree) if s is not None]
            kind = _kind(_literal_text(node.args[0], scopes))
            if kind != "other":
                yield getattr(function, "name", "<module>"), node.lineno, kind
        for child in ast.iter_child_nodes(node):
            yield from visit(child, function, guarded)

    yield from visit(tree, None, False)


def _package_findings() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    # The user store sits outside the package and writes to the same file.
    for path in [*sorted(_MEMORY.glob("*.py")), _MEMORY.parent / "participant.py"]:
        # A migration's statements run under the runner, which rolls back.
        if path.name.startswith("_migration_") or path.name == "_write_boundary.py":
            continue
        for function, line, kind in _unguarded(path.read_text(encoding="utf-8")):
            found.setdefault(f"{path.name}::{function}", []).append((line, kind))
    return found


def test_every_memory_write_ends_its_transaction() -> None:
    reviewed = _ENDED_ELSEWHERE.keys() | _NOT_A_WRITE.keys()
    unreviewed = {
        site: hits for site, hits in _package_findings().items() if site not in reviewed
    }
    assert not unreviewed, (
        "These statements in agents/memory write (or build their SQL where "
        "this check cannot read it) outside execute_write, write_boundary "
        "and @ends_transaction, so a path that skips their commit keeps "
        f"the database's write lock (ISSUE-0197): {unreviewed}"
    )


def test_the_reviewed_lists_name_only_statements_that_exist() -> None:
    """An entry whose function was renamed or fixed must be removed, or it
    would excuse the next function to take that name."""
    stale = (_ENDED_ELSEWHERE.keys() | _NOT_A_WRITE.keys()) - _package_findings().keys()
    assert not stale, f"remove these reviewed entries: {sorted(stale)}"
    assert all(
        kind == "unknown"
        for site in _NOT_A_WRITE
        for _, kind in _package_findings()[site]
    ), "a statement listed as not a write now reads as one"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('await db.execute("UPDATE t SET v = 1")', ["write"]),
        ('await db.execute("  delete from t")', ["write"]),
        ('await db.executemany("INSERT INTO t VALUES (?)", rows)', ["write"]),
        ('await db.execute_fetchall("INSERT INTO t VALUES (1) RETURNING id")', ["write"]),
        ('await db.execute("WITH x AS (SELECT 1) DELETE FROM t")', ["write"]),
        ('await db.execute(f"UPDATE t SET v = 1 WHERE id IN ({marks})")', ["write"]),
        ('sql = "DELETE FROM t"\n    await db.execute(sql)', ["write"]),
        ('sql = "DELETE " + "FROM t"\n    await db.execute(sql)', ["write"]),
        ("await db.execute(_UPDATE_SQL.format(x=1))", ["write"]),
        ("await db.execute(built_elsewhere())", ["unknown"]),
        ("sql, params = build()\n    await db.execute(sql, params)", ["unknown"]),
        ('await db.execute("SELECT 1")', []),
        ('await db.execute("PRAGMA journal_mode=WAL")', []),
        ('await execute_write(db, "UPDATE t SET v = 1")', []),
        ('async with write_boundary(db):\n        await db.execute("UPDATE t SET v = 1")', []),
        (
            'async with write_boundary(db):\n        pass\n'
            '    await db.execute("UPDATE t SET v = 1")',
            ["write"],
        ),
    ],
)
def test_the_reading_tells_a_guarded_write_from_a_bare_one(
    body: str, expected: list[str],
) -> None:
    source = f'_UPDATE_SQL = "UPDATE t SET v = {{x}}"\n\n\nasync def f(db):\n    {body}\n'
    assert [kind for _, _, kind in _unguarded(source)] == expected


def test_the_reading_accepts_a_decorated_function_and_not_its_neighbour() -> None:
    source = (
        "@ends_transaction\n"
        "async def owner(db):\n"
        '    await db.execute("UPDATE t SET v = 1")\n'
        "\n\n"
        "async def neighbour(db):\n"
        '    await db.execute("UPDATE t SET v = 1")\n'
    )
    assert [(name, kind) for name, _, kind in _unguarded(source)] == [
        ("neighbour", "write"),
    ]
