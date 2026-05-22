"""
PR #225 review regression pins for the procedural-tier confidence-decay
subsystem (RFC 0008 PR 5).

Split out of ``test_memory_decay.py`` only because adding these four
review-driven tests pushed the original file past the 500-line review
soft-cap enforced by ``scripts/checks/file_size.py --strict``.  The
split is also conceptually clean: this file pins findings from the
PR #225 deep-review report (round 1) — the original
file pins the original behavioural contracts.

Pins:

- **S1** — LIKE-wildcard escape on the ``refresh_confidence`` and
  ``recall_procedures`` paths.  A ``key`` / ``query`` containing the
  SQLite LIKE meta-characters (``%`` ``_``) must not widen the match.
- **S2** — recall / eviction agreement on legacy-shape rows
  (pre-PR-5 ``importance``-as-confidence rows that the v6 migration
  default left at ``confidence = 1.0``).
- **S4** — the documented silent discard of the ``confidence``
  argument on the ``store_procedure`` refresh path.
- **Mi1** (round 2) — ``_escape_like`` order-of-operation: backslash
  must be escaped before ``%`` / ``_`` so the helper does not
  double-escape its own inserted backslashes.  Pinned to surface a
  plausible alphabetical-cleanup regression.

When the ``_resolve_base_confidence`` shim is removed in PR 6 the S2
pin will need to either be retired or re-pinned at the new contract.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest

from agents.memory.decay import SECONDS_PER_DAY
from agents.memory.episodic_procedural import (
    _escape_like,
    recall_procedures,
    refresh_confidence,
)
from agents.memory.eviction import EvictionPass
from agents.memory.facade import MemoryStore


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryStore, None]:
    """Fresh in-memory facade per test (mirrors the ``test_memory_decay`` fixture)."""
    fac = MemoryStore(agent_id="proc-test", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


# ─── S4: silent confidence-arg discard on refresh path ───────


async def test_store_procedure_refresh_silently_discards_confidence_arg(
    facade: MemoryStore,
) -> None:
    """PR #225 review S4 regression pin.

    The refresh path discards both ``content`` *and* ``confidence``
    (docstring says so as of PR #225 round-1 fix).  This test pins the
    contract so a future change that starts honouring the supplied
    ``confidence`` on the refresh path is forced to update the
    docstring (and ideally bump the API surface — the discard is
    intentional today but is a known footgun).
    """
    await facade.store_procedure("kfix", "body", confidence=0.4)
    # Re-store with a *lower* confidence — an "honour" semantics would
    # write 0.2; the documented "refresh" semantics writes 1.0.
    await facade.store_procedure("kfix", "ignored-body", confidence=0.2)

    db = facade.episodic._ensure_db()  # noqa: SLF001
    async with db.execute(
        "SELECT confidence FROM episodes "
        "WHERE agent_id = ? AND tags_json LIKE '%\"procedure:kfix\"%'",
        ("proc-test",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == pytest.approx(1.0), (
        "refresh path must reset confidence to 1.0 regardless of "
        "the supplied ``confidence`` arg (PR #225 review S4)"
    )


# ─── S1: LIKE-wildcard escaping on key / query ───────────────


def test_escape_like_escapes_backslash_before_meta_chars() -> None:
    """PR #225 round-2 review Mi1 regression pin.

    ``_escape_like`` iterates ``_LIKE_META_CHARS = ("\\\\", "%", "_")``
    in that exact order — the backslash MUST be escaped first, otherwise
    the backslashes inserted by the subsequent ``%`` / ``_`` substitutions
    would themselves be re-escaped, producing ``\\\\%`` / ``\\\\_`` (literal
    backslash + literal meta-char) in the LIKE pattern instead of the
    intended ``\\%`` / ``\\_`` (escaped meta-char).  A reviewer reordering
    the tuple alphabetically (a plausible cleanup) would silently break
    every escape-paired LIKE clause.  Pinning the exact transform output
    makes the contract testable rather than implicit in tuple ordering.
    """
    assert _escape_like("a%b\\c_d") == "a\\%b\\\\c\\_d"


async def test_refresh_confidence_does_not_widen_match_on_percent_in_key(
    facade: MemoryStore,
) -> None:
    """A key containing ``%`` must not refresh sibling rows.

    Without LIKE-meta escaping (PR #225 review S1) ``key="50% off"``
    would build the LIKE pattern ``'%"procedure:50% off"%'`` and the
    inner ``%`` would match across the JSON boundary, refreshing
    every co-tenant procedure for the agent.  With escaping (and the
    paired ``ESCAPE '\\\\'`` clause) only the literal-key row matches.

    PR 6b (PR 5 R2 M1) note: the facade-level ``store_procedure`` /
    ``retrieve_procedures`` boundary now rejects keys with characters
    outside ``^[A-Za-z0-9._-]+$``, so this test exercises the
    helper-level :func:`refresh_confidence` directly to keep pinning
    the in-helper LIKE-escape behaviour as a defence-in-depth
    invariant — a future caller bypassing the facade (or a relaxation
    of the facade-side regex) must still see the escape work.
    """
    # Sibling row that must NOT be touched.
    await facade.store_procedure("sibling", "untouched", confidence=0.5)
    db = facade.episodic._ensure_db()  # noqa: SLF001
    # Forge a stale baseline on the sibling so a spurious refresh
    # would be observable (confidence would jump 0.5 → 1.0).
    await db.execute(
        "UPDATE episodes SET confidence = 0.5 WHERE agent_id = ?",
        ("proc-test",),
    )
    await db.commit()

    # Forge the target row directly (bypassing the facade-level key
    # validator) so the helper-level escape gets exercised end-to-end.
    forged_id = "forged-proc-id"
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, tags_json, "
        "confidence, importance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            forged_id,
            "proc-test",
            "body",
            '["procedure:50% off_promo"]',
            0.4,
            0.4,
            0.0,
        ),
    )
    await db.commit()

    # Refresh the sibling under the literal target key — only the
    # target's row should reset to 1.0.
    refreshed = await refresh_confidence(db, "proc-test", "50% off_promo")
    assert refreshed is True

    async with db.execute(
        "SELECT tags_json, confidence FROM episodes WHERE agent_id = ?",
        ("proc-test",),
    ) as cur:
        rows = list(await cur.fetchall())

    by_tag = {row[0]: row[1] for row in rows}
    target_tag = next(t for t in by_tag if "50% off_promo" in t)
    sibling_tag = next(t for t in by_tag if "sibling" in t)
    assert by_tag[target_tag] == pytest.approx(1.0)
    assert by_tag[sibling_tag] == pytest.approx(0.5), (
        "sibling row must not be refreshed when key contains '%' / '_' "
        "(PR #225 review S1)"
    )


async def test_recall_procedures_does_not_widen_match_on_percent_in_query(
    facade: MemoryStore,
) -> None:
    """A ``query`` containing ``%`` must match literally, not as a wildcard.

    Without escaping (PR #225 review S1) ``query="100% sure"`` would
    return rows whose summary contains ``"100"`` followed by anything
    and then ``" sure"``.  With escaping the search is literal.
    """
    await facade.store_procedure("a", "100% sure deploy", confidence=0.9)
    await facade.store_procedure("b", "100 maybe sure deploy", confidence=0.9)

    results = await facade.retrieve_procedures(query="100% sure")
    keys = {e.key for e in results}
    assert keys == {"a"}, (
        "literal '%' in query must not act as a wildcard "
        "(PR #225 review S1)"
    )


# ─── S2: recall / eviction agreement on legacy-shape rows ────


async def test_eviction_uses_legacy_base_confidence_shim() -> None:
    """PR #225 review S2 regression pin.

    Forges a pre-PR-5-shape row (``confidence`` at the v6 migration
    DEFAULT 1.0, ``importance`` carrying the real authored
    confidence) and verifies that ``_evict_procedural_decay``
    resolves the decay base via the same legacy-row shim that
    ``recall_procedures`` uses.  Without the shim, eviction would
    decay the row from a fresh ``1.0`` baseline and a row that
    recall correctly drops would silently survive eviction.
    """
    fac = MemoryStore(agent_id="legacy-evict", db_path=":memory:")
    await fac.initialize()
    try:
        db = fac.episodic._ensure_db()  # noqa: SLF001
        # Pre-PR-5 row: importance = 0.05 (the authored confidence),
        # confidence = 1.0 (v6 DEFAULT), backdated 5 days.  Without
        # the shim the eviction pass sees base=1.0 and decayed≈0.95
        # (above c_min=0.1) and keeps the row.  With the shim,
        # base=0.05 < c_min=0.1 immediately and the row is evicted
        # — matching what ``recall_procedures`` already does.
        await db.execute(
            "INSERT INTO episodes (id, agent_id, summary, tags_json, "
            "importance, created_at, access_count, "
            "confidence, last_validated_at) "
            "VALUES ('legacy1', 'legacy-evict', 'old', "
            "'[\"procedure:legacy-key\"]', 0.05, ?, 0, 1.0, NULL)",
            (time.time() - 5.0 * SECONDS_PER_DAY,),
        )
        await db.commit()

        # Recall must drop it (legacy shim already in place pre-S2).
        recall = await fac.retrieve_procedures()
        assert recall == [], "recall already drops the legacy stale row"

        runner = EvictionPass(
            "legacy-evict", episodic_cap=100, ttl_low_importance_days=30,
        )
        stats = await runner.run(db)
        assert stats.procedural_evicted == 1, (
            "eviction must agree with recall on legacy-shape rows "
            "(PR #225 review S2)"
        )
    finally:
        await fac.close()


# ─── PR 6b deep-review Should-Fix #4: SQL pre-filter cutoff ──


async def test_recall_procedures_sql_cutoff_filters_stale_rows_and_appears_in_sql(
    facade: MemoryStore,
) -> None:
    """Pin the new SQL-side ``t_max`` pre-filter clause introduced in PR 6b.

    PR 6b deep-review Should-Fix #4: ``recall_procedures`` now pushes
    ``t_max = -ln(c_min) / lambda_per_day`` into the SQL ``WHERE``
    clause as an over-fetch optimisation.  Because the in-Python decay
    loop *also* re-filters by ``c_min``, a regression that silently
    dropped (or no-op'd) the SQL clause would not change the returned
    result set — only a query-shape assertion can catch it.

    This test does both: inserts a row well beyond ``t_max`` and pins
    that (a) the helper returns ``[]`` and (b) the executed SQL
    contains the cutoff clause.  Mirrored disabled-decay assertion
    pins the documented "no cutoff when ``lambda_per_day == 0``"
    branch.
    """
    db = facade.episodic._ensure_db()  # noqa: SLF001
    # Row anchored 100 days ago — under DEFAULT_LAMBDA_PER_DAY=0.01 the
    # decayed confidence on a base of 1.0 is exp(-1.0) ≈ 0.368, but
    # under c_min=0.5 (chosen here) the cutoff t_max collapses to
    # ≈ 69 days, so a 100-day-old row must be SQL-filtered.
    forged_id = "stale-proc"
    anchor = time.time() - 100.0 * SECONDS_PER_DAY
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, tags_json, "
        "confidence, importance, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            forged_id,
            "proc-test",
            "stale body",
            '["procedure:stale-key"]',
            1.0,
            1.0,
            anchor,
        ),
    )
    await db.commit()

    captured: list[str] = []
    # ``aiosqlite.Connection.set_trace_callback`` is a coroutine
    # (unlike the underlying ``sqlite3`` sync method) — must be awaited.
    # Its stub types the callback as non-optional, but passing ``None``
    # clears it (as ``sqlite3`` documents); the ``arg-type`` ignores on
    # the ``None`` calls below cover that stub gap.
    await db.set_trace_callback(captured.append)
    try:
        result = await recall_procedures(
            db, "proc-test", c_min=0.5, lambda_per_day=0.01,
        )
    finally:
        await db.set_trace_callback(None)  # type: ignore[arg-type]
    assert result == [], "stale row must be filtered (regardless of SQL/Python path)"
    assert any(
        "COALESCE(last_validated_at, created_at)" in s for s in captured
    ), (
        "PR 6b: SQL-side cutoff clause must be in the executed query "
        "when both ``c_min`` and ``lambda_per_day`` are positive "
        "(deep-review Should-Fix #4)"
    )

    # Disabled-decay branch: ``lambda_per_day == 0`` must omit the
    # cutoff (division-by-zero guard) — pin the absence so a future
    # refactor that always emits the clause does not silently break
    # the disabled-decay deployment path.
    captured.clear()
    await db.set_trace_callback(captured.append)
    try:
        await recall_procedures(
            db, "proc-test", c_min=0.5, lambda_per_day=0.0,
        )
    finally:
        await db.set_trace_callback(None)  # type: ignore[arg-type]
    assert not any(
        "COALESCE(last_validated_at, created_at)" in s for s in captured
    ), (
        "PR 6b: SQL-side cutoff must be omitted when ``lambda_per_day`` "
        "is zero (avoids ``log(0)`` / divide-by-zero)"
    )


# ─── PR 6b deep-review Should-Fix #5: facade ``now=`` injection ──


async def test_retrieve_procedures_honours_now_override(
    facade: MemoryStore,
) -> None:
    """Pin the new ``now`` parameter on ``MemoryStore.retrieve_procedures``.

    PR 6b deep-review Should-Fix #5: the ``now: float | None``
    parameter (PR 5 R1 L4) is plumbed through the facade so tests can
    pin decay behaviour without freezing wall-clock.  Without this
    pin, the only test of ``now=`` is at the helper level — the
    facade-level wiring could regress (e.g. dropped from kwargs)
    silently.
    """
    await facade.store_procedure("nowtest", "body", confidence=1.0)
    # Sanity: with current wall-clock, the row is admitted.
    fresh = await facade.retrieve_procedures()
    assert any(e.key == "nowtest" for e in fresh)
    # Advance ``now`` 10_000 days into the future — under the default
    # decay (lambda=0.01/day, c_min=0.1) the decayed confidence is
    # exp(-100) ≈ 0, well below c_min, so the row must be dropped.
    far_future = time.time() + 10_000.0 * SECONDS_PER_DAY
    stale = await facade.retrieve_procedures(now=far_future)
    assert all(e.key != "nowtest" for e in stale), (
        "``now=`` override must be honoured by retrieve_procedures "
        "(PR 6b deep-review Should-Fix #5)"
    )
