"""
Episodic-tier eviction for memory-enabled task agents (RFC 0008 PR plan PR 2a).

Phase 2 implements two policies (RFC 0008 §G):

1. **TTL eviction** — entries with ``importance < 0.3`` whose ``created_at``
   is older than the configured ``ttl_low_importance_days`` are deleted.
2. **Size-cap eviction** — when the agent's episode count exceeds
   ``episodic_cap``, the lowest-scoring excess entries are deleted.  Score
   is the RFC 0008 §G hybrid::

       score = importance * 0.6 + recency_norm * 0.3 + access_freq_norm * 0.1

   ``recency_norm`` and ``access_freq_norm`` are normalised in ``[0, 1]``
   over the agent's current candidate set.  Ties are broken
   deterministically by ``created_at ASC`` so test fixtures are stable.

Confidence decay for the procedural tier is **out of scope** for this PR
and lands in PR 5 (RFC 0008 PR plan Phase 4b).  To keep the two policies
cleanly separated, every query in this module excludes rows whose
``tags_json`` carries the ``procedure:`` prefix written by
:meth:`MemoryFacade.store_procedure` — see ``_NOT_PROCEDURE_PREDICATE``
below and PR #221 deep-review finding M-1.

The :class:`EvictionPass` class is intentionally stateless across runs —
:meth:`EvictionPass.run` opens fresh queries against the agent's database
and returns an :class:`EvictionStats` report so the caller (the
:class:`~agents.memory.facade.MemoryFacade` background loop) can log /
trace each pass.  ``EvictionStats.total_after`` therefore reports the
*evictable* (episodic) row count — procedure rows are intentionally
excluded so the figure matches what the size-cap budget enforces.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiosqlite

from .decay import (
    DEFAULT_C_MIN,
    DEFAULT_LAMBDA_PER_DAY,
    compute_decayed_confidence,
)

# PR #225 review S2: share the legacy-row base-confidence shim with the
# recall path so a row's eviction disposition cannot disagree with its
# recall disposition (a legacy row whose ``importance`` is the real
# authored confidence must decay from that value in *both* paths).
from .episodic_procedural import resolve_base_confidence

logger = logging.getLogger(__name__)


# RFC 0008 §G — entries below this importance are eligible for TTL eviction.
TTL_IMPORTANCE_THRESHOLD: float = 0.3

# Hybrid-score weights (RFC 0008 §G).  Frozen for v0.3.0; any tuning
# requires an RFC amendment.
_W_IMPORTANCE: float = 0.6
_W_RECENCY: float = 0.3
_W_ACCESS: float = 0.1

# RFC 0008 §G separates episodic eviction from procedural confidence
# decay (the latter lands in PR 5).  ``MemoryFacade.store_procedure``
# persists procedures as episode rows tagged ``procedure:{key}`` with
# ``confidence`` mapped onto ``importance``; without this guard a
# low-confidence procedure would be silently TTL- or cap-evicted by the
# episodic policy.  ``tags_json`` is JSON-serialised by
# ``EpisodicMemory.store_episode`` (``json.dumps(tags or [])``) so the
# tag appears verbatim as ``"procedure:..."`` in the column and the
# ``LIKE`` pattern is collation-safe.  Legacy rows with
# ``tags_json IS NULL`` are evictable as before.  See PR #221 deep-review
# finding M-1.
_NOT_PROCEDURE_PREDICATE: str = (
    "(tags_json IS NULL OR tags_json NOT LIKE '%\"procedure:%')"
)


@dataclass(frozen=True)
class EvictionStats:
    """Per-pass eviction telemetry.

    PR 6b (PR 5 R2 N2): ``procedural_evicted`` is now required at
    construction so a future caller cannot accidentally drop the
    procedural-tier row count by relying on the previous default of
    ``0``.  The only construction site (:meth:`EvictionPass.run`)
    already supplies the value explicitly.
    """

    ttl_evicted: int
    cap_evicted: int
    total_after: int
    procedural_evicted: int


class EvictionPass:
    """Single eviction run against an open ``aiosqlite`` connection.

    Stateless — does not retain rows across runs.  The
    :class:`~agents.memory.facade.MemoryFacade` schedules one instance
    per cadence tick.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        episodic_cap: int,
        ttl_low_importance_days: int,
        lambda_per_day: float = DEFAULT_LAMBDA_PER_DAY,
        c_min: float = DEFAULT_C_MIN,
    ) -> None:
        if episodic_cap < 1:
            raise ValueError(
                f"episodic_cap must be >= 1, got {episodic_cap}",
            )
        if ttl_low_importance_days < 1:
            raise ValueError(
                f"ttl_low_importance_days must be >= 1, got "
                f"{ttl_low_importance_days}",
            )
        self._agent_id = agent_id
        self._episodic_cap = episodic_cap
        self._ttl_seconds = ttl_low_importance_days * 86400.0
        self._lambda_per_day = lambda_per_day
        self._c_min = c_min

    async def run(self, db: aiosqlite.Connection) -> EvictionStats:
        """Execute one eviction pass and return the stats report."""
        ttl_evicted = await self._evict_ttl(db)
        cap_evicted = await self._evict_size_cap(db)
        procedural_evicted = await self._evict_procedural_decay(db)
        total_after = await self._count(db)
        return EvictionStats(
            ttl_evicted=ttl_evicted,
            cap_evicted=cap_evicted,
            total_after=total_after,
            procedural_evicted=procedural_evicted,
        )

    # ─── TTL eviction ───────────────────────────────────────────

    async def _evict_ttl(self, db: aiosqlite.Connection) -> int:
        """Delete low-importance entries past the TTL window.

        Procedure rows are excluded — see ``_NOT_PROCEDURE_PREDICATE``.
        """
        cutoff = time.time() - self._ttl_seconds
        cursor = await db.execute(
            "DELETE FROM episodes "
            "WHERE agent_id = ? AND importance < ? AND created_at < ? "
            f"AND {_NOT_PROCEDURE_PREDICATE}",
            (self._agent_id, TTL_IMPORTANCE_THRESHOLD, cutoff),
        )
        deleted = cursor.rowcount or 0
        await db.commit()
        return deleted

    # ─── Size-cap eviction ──────────────────────────────────────

    async def _evict_size_cap(self, db: aiosqlite.Connection) -> int:
        """Delete the lowest-scoring excess entries above ``episodic_cap``.

        Procedure rows are excluded from both the budget count and the
        candidate set — see ``_NOT_PROCEDURE_PREDICATE``.
        """
        total = await self._count(db)
        excess = total - self._episodic_cap
        if excess <= 0:
            return 0
        # Pull the columns required for the hybrid score.  Normalisation
        # ranges (recency, access) are computed across the full candidate
        # set so ranking is consistent within a single pass.
        async with db.execute(
            "SELECT id, importance, created_at, access_count "
            f"FROM episodes WHERE agent_id = ? AND {_NOT_PROCEDURE_PREDICATE}",
            (self._agent_id,),
        ) as cur:
            rows = list(await cur.fetchall())
        if not rows:
            return 0
        scored = _score_rows(rows)
        # Lowest score first; tie-break by created_at ASC so the oldest
        # entry within a tied score-bucket is evicted first.
        scored.sort(key=lambda r: (r["score"], r["created_at"]))
        victims = scored[:excess]
        if not victims:
            return 0
        # SQLite's parameter substitution does not expand sequences in
        # an IN(...) clause; build the placeholders manually.  IDs are
        # UUID strings produced by ``store_episode`` — never user input —
        # so no SQL-injection surface even if the placeholder build path
        # were bypassed.
        ids = [v["id"] for v in victims]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM episodes WHERE agent_id = ? AND id IN ({placeholders})",
            (self._agent_id, *ids),
        )
        await db.commit()
        return len(victims)

    # ─── Procedural-tier decay eviction (RFC 0008 PR 5) ─────

    async def _evict_procedural_decay(
        self, db: aiosqlite.Connection,
    ) -> int:
        """Delete procedural rows whose decayed confidence is below ``c_min``.

        Procedural rows live in the same ``episodes`` table tagged
        ``procedure:{key}`` (see ``_NOT_PROCEDURE_PREDICATE``).  Their
        eviction policy is **not** the episodic hybrid score — it is the
        confidence-decay model from RFC 0008 §G:

        ``c_t = confidence * exp(-lambda_per_day * age_days)``

        where ``age_days`` is measured from ``last_validated_at`` if set,
        else ``created_at`` (so a never-revalidated procedure decays
        from its creation, while a refreshed procedure restarts the
        clock at 1.0 — see ``episodic_procedural.refresh_confidence``).
        """
        async with db.execute(
            # PR #225 review S2: select ``importance`` alongside
            # ``confidence`` so the legacy-row shim
            # (``_resolve_base_confidence``) can prefer ``importance``
            # whenever a pre-PR-5 row carries the v6 migration default
            # (``confidence = 1.0``) but a non-default ``importance``.
            # Without this join the eviction pass would decay legacy
            # rows from a fresh ``1.0`` baseline while recall decays
            # them from the authored ``importance`` — and the same row
            # could be admitted by recall yet survive eviction (or vice
            # versa once the shim is removed in PR 6).
            "SELECT id, confidence, last_validated_at, created_at, importance "
            "FROM episodes "
            "WHERE agent_id = ? AND tags_json LIKE '%\"procedure:%'",
            (self._agent_id,),
        ) as cur:
            rows = list(await cur.fetchall())
        if not rows:
            return 0
        now = time.time()
        victims: list[str] = []
        for r in rows:
            base = resolve_base_confidence(r[1], r[4])
            anchor = r[2] if r[2] is not None else r[3]
            age_seconds = max(0.0, now - float(anchor))
            decayed = compute_decayed_confidence(
                base, age_seconds, self._lambda_per_day,
            )
            if decayed < self._c_min:
                victims.append(r[0])
        if not victims:
            return 0
        placeholders = ",".join("?" for _ in victims)
        await db.execute(
            f"DELETE FROM episodes WHERE agent_id = ? AND id IN ({placeholders})",
            (self._agent_id, *victims),
        )
        await db.commit()
        return len(victims)

    # ─── Helpers ────────────────────────────────────────────────

    async def _count(self, db: aiosqlite.Connection) -> int:
        """Count *evictable* (non-procedure) rows for this agent.

        Procedure rows are excluded so the size-cap budget and the
        ``EvictionStats.total_after`` telemetry both reflect the
        episodic working set the cap actually governs.
        """
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE agent_id = ? "
            f"AND {_NOT_PROCEDURE_PREDICATE}",
            (self._agent_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0


def _score_rows(rows: list[aiosqlite.Row]) -> list[dict]:
    """Compute the RFC 0008 §G hybrid score for each row.

    Returns a list of dicts with keys ``id``, ``score``, ``created_at``
    so the caller can sort and slice without re-indexing positional rows.
    """
    if not rows:
        return []
    importances = [float(r[1]) for r in rows]
    created_ats = [float(r[2]) for r in rows]
    access_counts = [int(r[3] or 0) for r in rows]
    # Recency norm: oldest → 0.0, newest → 1.0.  Single-row or
    # all-equal-timestamp candidate sets collapse to 1.0 for every row
    # (there is no information to discriminate on).
    rec_min, rec_max = min(created_ats), max(created_ats)
    rec_span = rec_max - rec_min
    # Access-frequency norm: 0 → 0.0, max → 1.0.  Same all-equal handling.
    acc_max = max(access_counts)
    out: list[dict] = []
    for i, r in enumerate(rows):
        recency_norm = (
            (created_ats[i] - rec_min) / rec_span if rec_span > 0 else 1.0
        )
        access_norm = (
            access_counts[i] / acc_max if acc_max > 0 else 0.0
        )
        score = (
            importances[i] * _W_IMPORTANCE
            + recency_norm * _W_RECENCY
            + access_norm * _W_ACCESS
        )
        out.append({
            "id": r[0],
            "score": score,
            "created_at": created_ats[i],
        })
    return out


# ─── Background loop helper ────────────────────────────────────


async def eviction_loop(
    agent_id: str,
    db: aiosqlite.Connection,
    *,
    episodic_cap: int,
    ttl_low_importance_days: int,
    cadence_seconds: float,
    lambda_per_day: float = DEFAULT_LAMBDA_PER_DAY,
    c_min: float = DEFAULT_C_MIN,
) -> None:
    """Periodic eviction loop scheduled by ``MemoryFacade.initialize()``.

    Runs one :class:`EvictionPass` every ``cadence_seconds``.  Failures
    log a warning and the loop continues — eviction is best-effort
    (mirrors RFC 0005 working-memory async-flush pattern).  The loop
    exits cleanly when cancelled by ``MemoryFacade.close()``.
    """
    if cadence_seconds <= 0:
        raise ValueError(
            f"cadence_seconds must be positive, got {cadence_seconds}",
        )
    pass_runner = EvictionPass(
        agent_id,
        episodic_cap=episodic_cap,
        ttl_low_importance_days=ttl_low_importance_days,
        lambda_per_day=lambda_per_day,
        c_min=c_min,
    )
    logger.info(
        "Eviction loop started for agent %s (cadence=%.0fs, cap=%d, ttl=%dd)",
        agent_id, cadence_seconds, episodic_cap, ttl_low_importance_days,
    )
    # PR 2a M-2: run the first pass after a short startup delay so that
    # over-cap agents don't stay bloated for a full cadence after restart.
    # Subsequent passes keep the full cadence period.
    startup_delay = min(60.0, cadence_seconds / 10.0)
    try:
        await asyncio.sleep(startup_delay)
        while True:
            try:
                stats = await pass_runner.run(db)
                if stats.ttl_evicted or stats.cap_evicted or stats.procedural_evicted:
                    logger.info(
                        "Eviction pass for %s: ttl=%d cap=%d procedural=%d remaining=%d",
                        agent_id, stats.ttl_evicted, stats.cap_evicted,
                        stats.procedural_evicted, stats.total_after,
                    )
            except Exception:  # noqa: BLE001 — best-effort; loop must survive
                logger.warning(
                    "Eviction pass failed for agent %s — loop continues",
                    agent_id, exc_info=True,
                )
            await asyncio.sleep(cadence_seconds)
    except asyncio.CancelledError:
        logger.info("Eviction loop cancelled for agent %s", agent_id)
        raise


__all__ = [
    "EvictionPass",
    "EvictionStats",
    "TTL_IMPORTANCE_THRESHOLD",
    "eviction_loop",
]
