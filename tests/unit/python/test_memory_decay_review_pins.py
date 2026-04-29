"""
PR #225 review regression pins for the procedural-tier confidence-decay
subsystem (RFC 0008 PR 5).

Split out of ``test_memory_decay.py`` only because adding these four
review-driven tests pushed the original file past the 500-line review
soft-cap enforced by ``scripts/checks/file_size.py --strict``.  The
split is also conceptually clean: this file pins findings from
``docs/pr-reviews/pr-225-deep-review.md`` (round 1) — the original
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
    refresh_confidence,
)
from agents.memory.eviction import EvictionPass
from agents.memory.facade import MemoryFacade


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryFacade, None]:
    """Fresh in-memory facade per test (mirrors the ``test_memory_decay`` fixture)."""
    fac = MemoryFacade(agent_id="proc-test", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


# ─── S4: silent confidence-arg discard on refresh path ───────


async def test_store_procedure_refresh_silently_discards_confidence_arg(
    facade: MemoryFacade,
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
    facade: MemoryFacade,
) -> None:
    """A key containing ``%`` must not refresh sibling rows.

    Without LIKE-meta escaping (PR #225 review S1) ``key="50% off"``
    would build the LIKE pattern ``'%"procedure:50% off"%'`` and the
    inner ``%`` would match across the JSON boundary, refreshing
    every co-tenant procedure for the agent.  With escaping (and the
    paired ``ESCAPE '\\\\'`` clause) only the literal-key row matches.
    """
    # Sibling row that must NOT be touched.
    await facade.store_procedure("sibling", "untouched", confidence=0.5)
    # Forge a stale baseline on the sibling so a spurious refresh
    # would be observable (confidence would jump 0.5 → 1.0).
    db = facade.episodic._ensure_db()  # noqa: SLF001
    await db.execute(
        "UPDATE episodes SET confidence = 0.5 WHERE agent_id = ?",
        ("proc-test",),
    )
    await db.commit()

    # Target row whose key contains LIKE-meta characters.
    await facade.store_procedure("50% off_promo", "body", confidence=0.4)
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
    facade: MemoryFacade,
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
    fac = MemoryFacade(agent_id="legacy-evict", db_path=":memory:")
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
