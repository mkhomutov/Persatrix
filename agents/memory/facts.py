"""
Declarative-fact tier (RFC 0026 PR 1).

Per-agent storage of structured ``(subject, predicate, object)`` tuples
extracted from interaction summaries — the load-bearing fix for the
[dementia test](../../docs/manual-tests/MT-MEMORY-005-dementia-test.md)
named-entity / preference / self-consistency legs.

PR 1 ships the storage primitive only.  PR 2 wires the extractor at
interaction close (combined summarize + extract prompt); PR 3 wires
``FactStore.recall`` into the ``MemoryBudget`` allocator; PR 4 adds
use-based reinforcement and the latest-asserted-wins retraction policy
on top of the ``superseded_by`` column reserved here.

The ``delete_by_subject`` primitive is the RFC 0013 ``SubjectErasure``
traversal point — see :meth:`FactStore.delete_by_subject` for the
GDPR / CCPA contract.  The umbrella ``SubjectErasure.delete`` wiring is
RFC 0013's responsibility (target v0.5.0); shipping the primitive now
prevents the first erasure request after v0.3.1 from silently missing
extracted facts.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import aiosqlite

from ..observability.metrics import try_get_instruments
from .migrations import _apply_migrations

logger = logging.getLogger(__name__)

__all__ = ["Fact", "FactStore"]


# Recall limit ceiling — mirrors :data:`agents.memory.notes._MAX_RECALL_LIMIT`
# so the persona runtime cannot pull an unbounded result set into a single
# prompt (RFC 0017 budget allocator owns the per-tier slice; this is the
# hard upper bound on row count regardless of token shape).
_MAX_RECALL_LIMIT = 100


# ─── Data model ─────────────────────────────────────────────


@dataclass
class Fact:
    """A single declarative-fact tuple — RFC 0026 §A data shape.

    ``fact_id`` is a uuid4 hex string in this implementation; the RFC
    text names ULID but the rest of the storage layer (``Episode.id``,
    ``Note.id``, ``Interaction.id``) uses ``uuid.uuid4`` and the
    asserted-at column already gives chronological ordering, so the
    ULID time-prefix property is not load-bearing here.

    ``certainty`` is seeded by the extractor (PR 2) and decayed /
    reinforced by PR 4's use-based salience rule.  PR 1 stores whatever
    value the caller supplies (default ``1.0``) — recall does not yet
    apply a salience score.

    ``superseded_by`` carries the ``fact_id`` of the row that replaces
    this one under latest-asserted-wins retraction.  PR 4 owns the
    recall-side policy; PR 1 ships the column + the write path so the
    schema is forward-compatible.

    ``session_id`` mirrors the migration-v7 contract on episodes /
    relationships — pre-RFC-0031 callers produce queryable rows with
    the ``'legacy'`` synthetic carve-out.
    """

    fact_id: str
    agent_id: str
    subject: str
    predicate: str
    object: str
    certainty: float
    source_interaction_id: str | None
    asserted_at: float
    last_recalled_at: float | None
    superseded_by: str | None
    session_id: str


# Column list pinned here so :meth:`FactStore._row_to_fact` stays in
# sync with SELECT statements — same pattern as ``_NOTE_COLS`` in
# :mod:`agents.memory.notes`.
_FACT_COLS = (
    "fact_id",
    "agent_id",
    "subject",
    "predicate",
    "object",
    "certainty",
    "source_interaction_id",
    "asserted_at",
    "last_recalled_at",
    "superseded_by",
    "session_id",
)
_FACT_SELECT = ", ".join(_FACT_COLS)


# ─── Predicate validation seam ──────────────────────────────


PredicateValidator = Callable[[str], None]


def _permissive_validator(predicate: str) -> None:
    """Phase-1 default validator — accepts any non-empty predicate.

    PR 2 swaps this for the enumerated allowlist (≈30 verbs across
    attribute / preference / commitment / relationship + ``self.*``
    classes per RFC 0026 §B + OQ #10).  The seam is exercised in
    :mod:`tests.unit.python.test_fact_store` so the PR 2 swap is a
    one-line change with regression coverage.
    """
    if not predicate or not predicate.strip():
        raise ValueError("predicate must not be empty")


# ─── FactStore ──────────────────────────────────────────────


class FactStore:
    """Per-agent CRUD over the ``facts`` table.

    Mirrors :class:`agents.memory.episodic.EpisodicMemory`'s connection
    + migration shape: each instance owns an ``aiosqlite`` connection
    against ``db_path`` and runs the shared migration umbrella at
    :meth:`initialize` time.  Alternatively, a caller can pass
    ``shared_db`` to reuse an existing connection (e.g. an
    ``EpisodicMemory`` already holding the file open) — useful for the
    cross-tier ``:memory:`` test patterns where two stores must share
    state, and for the PR 2 extractor which already operates on the
    persona-runtime's connection.
    """

    def __init__(
        self,
        agent_id: str,
        db_path: str = "data/memory.db",
        *,
        shared_db: aiosqlite.Connection | None = None,
        predicate_validator: PredicateValidator | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = shared_db
        self._owns_db = shared_db is None
        self._predicate_validator = predicate_validator or _permissive_validator

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def initialize(self) -> None:
        """Open the database (when not sharing) and run migrations.

        Idempotent when the caller passed ``shared_db`` — migrations
        are owned by the original connection's owner in that case, but
        re-running the umbrella is safe (the ``schema_version`` table
        gates each migration).
        """
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
        await _apply_migrations(self._db)

    async def close(self) -> None:
        """Close the database connection if this store opened it."""
        if self._db is not None and self._owns_db:
            await self._db.close()
        self._db = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(
                "FactStore not initialised — call initialize() first",
            )
        return self._db

    # ─── Write path ────────────────────────────────────────

    async def store(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,  # noqa: A002 — RFC 0026 §A names this field literally
        source_interaction_id: str | None,
        asserted_at: float,
        certainty: float = 1.0,
        session_id: str = "legacy",
    ) -> str:
        """Persist a new fact tuple.  Returns the generated ``fact_id``.

        If a row with the same ``(agent_id, subject, predicate)`` and an
        older ``asserted_at`` already exists and is not yet superseded,
        the older row's ``superseded_by`` column is updated to point at
        the new row — RFC 0026 §F latest-asserted-wins retraction
        (storage half).  The recall-side filter that hides superseded
        rows by default ships in PR 4; PR 1 lands the data shape.

        Predicate validation runs through the injected validator (PR 2
        wires the allowlist; PR 1 default is permissive but still
        rejects empty strings).
        """
        self._predicate_validator(predicate)
        if not subject or not subject.strip():
            raise ValueError("subject must not be empty")
        if not 0.0 <= certainty <= 1.0:
            raise ValueError(
                f"certainty must be in [0.0, 1.0], got {certainty}",
            )

        db = self._ensure_db()
        fact_id = str(uuid.uuid4())

        async with db.execute(
            """
            SELECT fact_id FROM facts
            WHERE agent_id = ?
              AND subject = ?
              AND predicate = ?
              AND superseded_by IS NULL
              AND asserted_at < ?
            ORDER BY asserted_at DESC
            LIMIT 1
            """,
            (self._agent_id, subject, predicate, asserted_at),
        ) as cursor:
            row = await cursor.fetchone()
        older_fact_id = row[0] if row else None

        await db.execute(
            """
            INSERT INTO facts
                (fact_id, agent_id, subject, predicate, object,
                 certainty, source_interaction_id, asserted_at,
                 last_recalled_at, superseded_by, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                fact_id,
                self._agent_id,
                subject,
                predicate,
                object,
                certainty,
                source_interaction_id,
                asserted_at,
                session_id,
            ),
        )

        superseded = False
        if older_fact_id is not None:
            await db.execute(
                "UPDATE facts SET superseded_by = ? "
                "WHERE fact_id = ? AND agent_id = ?",
                (fact_id, older_fact_id, self._agent_id),
            )
            superseded = True

        await db.commit()

        # Telemetry counters live outside the persistence path — a
        # metrics-backend failure must not surface as a write failure
        # (row is already persisted).  Mirrors the
        # ``EpisodicMemory.store_episode`` ``sessions.writes`` pattern.
        with contextlib.suppress(Exception):
            inst = try_get_instruments()
            if inst is not None:
                inst.facts_stored.add(
                    1, attributes={"agent.id": self._agent_id},
                )
                if superseded:
                    inst.facts_superseded.add(
                        1, attributes={"agent.id": self._agent_id},
                    )

        return fact_id

    # ─── Read path ─────────────────────────────────────────

    async def recall(
        self,
        *,
        subject: str,
        limit: int = 10,
        include_superseded: bool = False,
    ) -> list[Fact]:
        """Return facts about ``subject`` ordered most-recent-first.

        Filters by ``agent_id`` so cross-agent leakage is impossible
        (RFC 0008 §H ACL).  ``superseded_by IS NOT NULL`` rows are
        excluded by default — PR 4's retraction policy reuses this
        filter without further changes.

        ``limit`` is clamped to ``_MAX_RECALL_LIMIT`` (100).  PR 3 will
        compose this with the ``MemoryBudget`` allocator's per-tier
        token slice; this floor is the hard upper bound on row count.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        limit = min(limit, _MAX_RECALL_LIMIT)

        db = self._ensure_db()
        if include_superseded:
            sql = (
                f"SELECT {_FACT_SELECT} FROM facts "
                "WHERE agent_id = ? AND subject = ? "
                "ORDER BY asserted_at DESC LIMIT ?"
            )
        else:
            sql = (
                f"SELECT {_FACT_SELECT} FROM facts "
                "WHERE agent_id = ? AND subject = ? "
                "AND superseded_by IS NULL "
                "ORDER BY asserted_at DESC LIMIT ?"
            )
        async with db.execute(
            sql, (self._agent_id, subject, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    # ─── Retraction / cleanup ──────────────────────────────

    async def supersede(self, fact_id: str, by_fact_id: str) -> bool:
        """Manually mark ``fact_id`` as superseded by ``by_fact_id``.

        Returns ``True`` if a row was updated.  Storage-only helper —
        the latest-asserted-wins policy in :meth:`store` is the common
        path; this exists for callers (PR 4 + future RFC 0027
        consolidation) that need to retract a fact without writing a
        successor of identical ``(subject, predicate)``.
        """
        db = self._ensure_db()
        cursor = await db.execute(
            "UPDATE facts SET superseded_by = ? "
            "WHERE fact_id = ? AND agent_id = ? "
            "AND superseded_by IS NULL",
            (by_fact_id, fact_id, self._agent_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def prune(self, *, before: float) -> int:
        """Delete superseded facts asserted before ``before`` seconds.

        Retention primitive — PR 1 ships it so the operator-side cap
        (RFC 0008 §G eviction) has a callable target without waiting on
        PR 4.  Returns the count of deleted rows.  Only operates on
        superseded rows so live facts are never silently dropped.
        """
        db = self._ensure_db()
        cursor = await db.execute(
            "DELETE FROM facts WHERE agent_id = ? "
            "AND superseded_by IS NOT NULL AND asserted_at < ?",
            (self._agent_id, before),
        )
        await db.commit()
        return cursor.rowcount

    async def delete_by_subject(self, subject_id: str) -> dict[str, int]:
        """Erase every fact tied to ``subject_id`` — RFC 0013 §C / RFC 0026 §H.

        Traverses **both** the ``subject`` column (facts *about* the
        subject) and the ``source_interaction_id`` column (facts
        *extracted during* an interaction belonging to the subject —
        even if the declared subject is someone else).  The audit-map
        return shape names the two subtotals separately so the umbrella
        ``SubjectErasure.delete`` audit-log entry can render an honest
        per-column breakdown.

        Phase 1 ships the storage primitive only.  RFC 0013's
        ``SubjectErasure`` (target v0.5.0) will wire this into the
        umbrella ``records_deleted`` audit map.  Without this primitive,
        the first GDPR / CCPA request after v0.3.1 ships would silently
        miss extracted facts — see RFC 0026 §H.
        """
        db = self._ensure_db()
        cursor = await db.execute(
            "DELETE FROM facts WHERE agent_id = ? AND subject = ?",
            (self._agent_id, subject_id),
        )
        by_subject = cursor.rowcount
        cursor = await db.execute(
            "DELETE FROM facts WHERE agent_id = ? "
            "AND source_interaction_id = ?",
            (self._agent_id, subject_id),
        )
        by_source = cursor.rowcount
        await db.commit()
        return {
            "facts_deleted_by_subject": by_subject,
            "facts_deleted_by_source_interaction": by_source,
        }

    # ─── Internal helpers ──────────────────────────────────

    def _row_to_fact(self, row: aiosqlite.Row) -> Fact:
        return Fact(
            fact_id=row[0],
            agent_id=row[1],
            subject=row[2],
            predicate=row[3],
            object=row[4],
            certainty=row[5],
            source_interaction_id=row[6],
            asserted_at=row[7],
            last_recalled_at=row[8],
            superseded_by=row[9],
            session_id=row[10],
        )
