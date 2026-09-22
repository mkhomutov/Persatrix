"""Four procedural-tier bugs, pinned before they were fixed.

The procedural tier stores "how to do X" entries (RFC 0008 §G) that lose
confidence over time and regain it when reused.  Nothing in the product
calls it yet — only tests call ``MemoryStore.store_procedure`` and
``retrieve_procedures`` — so none of these reached a running persona.
The max review of PR #977 found them:

1. **A re-store in one session refreshed another session's row.**
   ``store_procedure`` first tries ``refresh_confidence``, whose UPDATE had
   no session clause, while recall reads only the active session plus
   ``legacy``.  Storing a key that another session already held refreshed
   that session's row, and because a refresh skips the insert, the new
   session's own row was never written.
2. **A refresh never reset the decay base.**  It set ``confidence`` to 1.0
   but left ``importance`` at the stored value, and
   :func:`~agents.memory.episodic_procedural.resolve_base_confidence` reads
   ``importance`` whenever ``confidence`` is 1.0.  A procedure stored at 0.4
   kept decaying from 0.4, and one stored below ``c_min`` could never come
   back: recall hid it and eviction deleted it.
3. **The recall's fixed LIMIT cut before the decay filter.**  Newer rows
   that decay below ``c_min`` filled the window and pushed out older rows
   that pass.
4. **The recall did not check ``limit``.**  A negative limit reached SQLite
   as ``LIMIT -1``, which means no limit, and ``0`` returned nothing.  The
   episodic and notes reads raise ``ValueError`` below 1 and cap at 100.

Every test here failed before the fix except
``test_restore_in_a_named_session_refreshes_the_legacy_row_it_recalls``,
which passes on both sides: it pins the ``legacy`` half of the session
rule so the fix cannot tighten into a duplicate row.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest

from agents.memory.decay import SECONDS_PER_DAY
from agents.memory.episodic_procedural import recall_procedures
from agents.memory.eviction import EvictionPass
from agents.memory.facade import MemoryStore
from agents.session_id import session_scope

AGENT = "proc-scope"


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryStore, None]:
    """A fresh store per test, in the ``legacy`` session (the conftest clears
    ``PERSATRIX_SESSION_ID``) with the default decay knobs:
    ``c_min = 0.1`` and ``lambda = 0.01`` per day."""
    fac = MemoryStore(agent_id=AGENT, db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


def _tag_pattern(key: str) -> str:
    """The LIKE pattern that matches one procedure's tag."""
    return f'%"procedure:{key}"%'


async def _forge(
    facade: MemoryStore, key: str, *, session_id: str | None = None, **cols: float,
) -> None:
    """Overwrite columns on the procedure rows for *key* (all sessions, or one)."""
    db = facade.episodic._ensure_db()  # noqa: SLF001 — tests forge row state
    assignments = ", ".join(f"{name} = ?" for name in cols)
    sql = (
        f"UPDATE episodes SET {assignments} "  # noqa: S608 — names from this file
        "WHERE agent_id = ? AND tags_json LIKE ?"
    )
    params: list[object] = [*cols.values(), AGENT, _tag_pattern(key)]
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    await db.execute(sql, tuple(params))
    await db.commit()


async def _row_state(
    facade: MemoryStore, key: str, session_id: str,
) -> tuple[float, float, float]:
    """Return ``(confidence, importance, last_validated_at)`` for one row."""
    db = facade.episodic._ensure_db()  # noqa: SLF001
    async with db.execute(
        "SELECT confidence, importance, last_validated_at FROM episodes "
        "WHERE agent_id = ? AND tags_json LIKE ? AND session_id = ?",
        (AGENT, _tag_pattern(key), session_id),
    ) as cursor:
        rows = list(await cursor.fetchall())
    assert len(rows) == 1, f"expected one {key!r} row in {session_id!r}"
    return float(rows[0][0]), float(rows[0][1]), float(rows[0][2])


async def _count_rows(facade: MemoryStore, key: str) -> int:
    db = facade.episodic._ensure_db()  # noqa: SLF001
    async with db.execute(
        "SELECT COUNT(*) FROM episodes WHERE agent_id = ? AND tags_json LIKE ?",
        (AGENT, _tag_pattern(key)),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# ─── 1. A re-store refreshes only rows its own session can see ──


async def test_restore_in_another_session_writes_its_own_row(
    facade: MemoryStore,
) -> None:
    """The review's repro: room-b re-storing room-a's key must end with
    room-b holding its own row, not with nothing."""
    with session_scope("room-a"):
        await facade.store_procedure("deploy", "a", confidence=0.9)
    with session_scope("room-b"):
        await facade.store_procedure("deploy", "b", confidence=0.9)
        got = await facade.retrieve_procedures()
    assert [e.content for e in got] == ["b"]


async def test_restore_in_another_session_leaves_the_first_row_alone(
    facade: MemoryStore,
) -> None:
    """room-b's store must not refresh room-a's row: room-a's confidence,
    importance and last validation stay where room-a left them."""
    with session_scope("room-a"):
        await facade.store_procedure("deploy", "a", confidence=0.9)
    validated = time.time() - 30.0 * SECONDS_PER_DAY
    await _forge(
        facade, "deploy", session_id="room-a", last_validated_at=validated,
    )
    with session_scope("room-b"):
        await facade.store_procedure("deploy", "b", confidence=0.9)
    assert await _row_state(facade, "deploy", "room-a") == (
        pytest.approx(0.9), pytest.approx(0.9), validated,
    )


async def test_restore_in_legacy_does_not_refresh_a_named_session_row(
    facade: MemoryStore,
) -> None:
    """The same rule from the other side: a store with no session scope
    writes to ``legacy``, which cannot see room-a's rows, so it must write
    its own row instead of refreshing room-a's."""
    with session_scope("room-a"):
        await facade.store_procedure("deploy", "a", confidence=0.9)
    await facade.store_procedure("deploy", "shared", confidence=0.9)
    got = await facade.retrieve_procedures()
    assert [e.content for e in got] == ["shared"]


async def test_restore_with_explicit_session_id_writes_to_that_session(
    facade: MemoryStore,
) -> None:
    """An explicit ``session_id`` decides where the row goes, so it must also
    decide which rows the refresh may touch — not the active scope."""
    with session_scope("room-a"):
        await facade.store_procedure("deploy", "a", confidence=0.9)
        await facade.store_procedure(
            "deploy", "c", confidence=0.9, session_id="room-c",
        )
    got = await facade.retrieve_procedures(sessions=["room-c"])
    assert [e.content for e in got] == ["c"]


async def test_restore_in_a_named_session_refreshes_the_legacy_row_it_recalls(
    facade: MemoryStore,
) -> None:
    """Every session recalls ``legacy`` rows, so a named session re-storing a
    key held only by a ``legacy`` row refreshes that row rather than adding a
    second one it would recall next to it.  (The facts tier's supersede chain
    treats ``legacy`` the same way — ISSUE-0079.)"""
    await facade.store_procedure("deploy", "shared", confidence=0.9)
    with session_scope("room-a"):
        await facade.store_procedure("deploy", "a", confidence=0.9)
        got = await facade.retrieve_procedures()
    assert [e.content for e in got] == ["shared"]
    assert await _count_rows(facade, "deploy") == 1


# ─── 2. A refresh restarts decay from 1.0 ──────────────────────


async def test_restore_resets_the_decay_base_to_one(facade: MemoryStore) -> None:
    await facade.store_procedure("deploy", "body", confidence=0.4)
    await facade.store_procedure("deploy", "body", confidence=0.4)
    got = await facade.retrieve_procedures()
    assert [e.base_confidence for e in got] == [pytest.approx(1.0)]


async def test_restore_revives_a_procedure_stored_below_c_min(
    facade: MemoryStore,
) -> None:
    """0.05 is below ``c_min``, so recall hides the row until a successful
    reuse refreshes it."""
    await facade.store_procedure("deploy", "body", confidence=0.05)
    assert await facade.retrieve_procedures() == []
    await facade.store_procedure("deploy", "body", confidence=0.05)
    got = await facade.retrieve_procedures()
    assert [e.key for e in got] == ["deploy"]


async def test_eviction_keeps_a_refreshed_procedure(facade: MemoryStore) -> None:
    """Eviction reads the same decay base as recall, so it must see the
    refreshed 1.0 too, not delete the row for its original 0.05."""
    await facade.store_procedure("deploy", "body", confidence=0.05)
    await facade.store_procedure("deploy", "body", confidence=0.05)
    runner = EvictionPass(AGENT, episodic_cap=100, ttl_low_importance_days=30)
    stats = await runner.run(facade.episodic._ensure_db())  # noqa: SLF001
    assert stats.procedural_evicted == 0
    assert await _count_rows(facade, "deploy") == 1


# ─── 3. Rows that fail the decay filter do not crowd out rows that pass ─


@pytest.mark.parametrize("query", ["", "deploy"])
async def test_newer_rows_below_c_min_do_not_hide_an_older_valid_row(
    facade: MemoryStore, query: str,
) -> None:
    """The review's repro, on both SQL branches (with and without a query):
    two newer rows stored at 0.05 must not stop ``limit=1`` from finding
    the older row stored at 1.0."""
    now = time.time()
    await facade.store_procedure("valid", "deploy the old way", confidence=1.0)
    await facade.store_procedure("weak-1", "deploy guess one", confidence=0.05)
    await facade.store_procedure("weak-2", "deploy guess two", confidence=0.05)
    for age_seconds, key in ((30.0, "valid"), (20.0, "weak-1"), (10.0, "weak-2")):
        await _forge(facade, key, created_at=now - age_seconds)
    got = await facade.retrieve_procedures(query, limit=1, now=now)
    assert [e.key for e in got] == ["valid"]


async def test_newer_rows_that_decayed_below_c_min_do_not_hide_an_older_valid_row(
    facade: MemoryStore,
) -> None:
    """Rows whose stored base passes ``c_min`` but whose decayed value does
    not must not fill the window either.  Stored at 0.5 and last validated
    200 days ago, they decay to 0.5 * exp(-2) ≈ 0.068 — below 0.1, yet
    inside the 230-day age cutoff the SQL applies for a base of 1.0.  So a
    fix that only moved a base-value check into SQL would still fail here."""
    now = time.time()
    await facade.store_procedure("valid", "old but revalidated", confidence=1.0)
    await _forge(facade, "valid", created_at=now - 300.0 * SECONDS_PER_DAY)
    for key in ("faded-1", "faded-2"):
        await facade.store_procedure(key, "once decent", confidence=0.5)
        await _forge(
            facade, key,
            created_at=now - 250.0 * SECONDS_PER_DAY,
            last_validated_at=now - 200.0 * SECONDS_PER_DAY,
        )
    got = await facade.retrieve_procedures(limit=1, now=now)
    assert [e.key for e in got] == ["valid"]


@pytest.mark.parametrize("tied_created_at", [False, True])
async def test_recall_reads_on_until_limit_rows_pass(
    facade: MemoryStore, tied_created_at: bool,
) -> None:
    """Ten rows, newest ``p0`` to oldest ``p9``; only p5, p8 and p9 pass the
    decay filter.  ``limit=2`` must return p5 then p8 — the two newest rows
    that pass, in recall order, with none skipped or repeated even when the
    rows passing are several windows deep.  With every ``created_at`` tied,
    insertion order alone decides the order (issue #740), so the tie case
    checks that it also decides where one window ends and the next begins."""
    now = time.time()
    passing = {"p5", "p8", "p9"}
    keys = [f"p{i}" for i in range(10)]
    # Store oldest first, so insertion order matches the intended order.
    for key in reversed(keys):
        confidence = 1.0 if key in passing else 0.05
        await facade.store_procedure(key, f"step {key}", confidence=confidence)
    for i, key in enumerate(keys):
        created_at = now - 60.0 if tied_created_at else now - 60.0 - i
        await _forge(facade, key, created_at=created_at)
    got = await facade.retrieve_procedures(limit=2, now=now)
    assert [e.key for e in got] == ["p5", "p8"]


# ─── 4. ``limit`` is checked like the sibling reads ───────────


@pytest.mark.parametrize("limit", [0, -1])
async def test_retrieve_procedures_rejects_limit_below_one(
    facade: MemoryStore, limit: int,
) -> None:
    await facade.store_procedure("deploy", "body", confidence=0.9)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await facade.retrieve_procedures(limit=limit)


async def test_recall_procedures_rejects_limit_below_one(
    facade: MemoryStore,
) -> None:
    """The check sits in the helper that builds the SQL, so a direct caller
    cannot send ``LIMIT -1`` either."""
    await facade.store_procedure("deploy", "body", confidence=0.9)
    db = facade.episodic._ensure_db()  # noqa: SLF001
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await recall_procedures(db, AGENT, limit=-1)


async def test_retrieve_procedures_caps_limit_at_100(facade: MemoryStore) -> None:
    for i in range(101):
        await facade.store_procedure(f"k{i:03d}", "body", confidence=0.9)
    got = await facade.retrieve_procedures(limit=500)
    assert len(got) == 100
