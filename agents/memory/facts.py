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
from collections.abc import Callable, Iterable

import aiosqlite

from ..epoch_id import resolve_epoch_id_silent
from ..observability.metrics import try_get_instruments
from ..principal_id import resolve_principal_id_silent
from ..session_id import normalize_session_id, resolve_session_id_silent
from ._epoch_filter import epoch_eq_clause, resolve_active_epoch
from ._facts_audit import emit_audit as _emit_audit
from ._facts_erasure import delete_by_subject as _delete_by_subject
from ._facts_reinforce import mark_recalled_for_agent as _mark_recalled_for_agent
from ._facts_supersede import apply_supersession as _apply_supersession
from ._facts_supersede import retract_fact as _retract_fact
from ._facts_topics import topic_subjects_for_agent as _topic_subjects_for_agent
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from ._principal_filter import principal_eq_clause, resolve_active_principal
from ._session_filter import _resolve_session_list, session_in_clause
from .fact_predicates import (
    canonicalize_subject,
    validate_object,
    validate_predicate,
)
from .fact_types import _FACT_COLS, _FACT_SELECT, Fact
from .migrations import _apply_migrations

logger = logging.getLogger(__name__)

# ``Fact`` / column constants moved to :mod:`agents.memory.fact_types`
# (ISSUE-0085 PR 3 — keep this module under the 500-line cap); re-exported
# here so ``from agents.memory.facts import Fact`` is unchanged.
__all__ = ["Fact", "FactStore", "_FACT_COLS", "_FACT_SELECT"]


# Recall limit ceiling — mirrors :data:`agents.memory.notes._MAX_RECALL_LIMIT`
# so the persona runtime cannot pull an unbounded result set into a single
# prompt (RFC 0017 budget allocator owns the per-tier slice; this is the
# hard upper bound on row count regardless of token shape).
_MAX_RECALL_LIMIT = 100


# Predicate validation seam — PR 2 swapped the Phase-1 permissive
# default for the enumerated allowlist in
# :mod:`agents.memory.fact_predicates`.  The Callable seam stays so a
# caller can still inject a custom validator without touching the
# storage layer.
PredicateValidator = Callable[[str], None]
_default_predicate_validator: PredicateValidator = validate_predicate


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
        self._predicate_validator = (
            predicate_validator or _default_predicate_validator
        )
        # RFC 0031 Phase 2 PR 3 — tier-owned active session (mirrors
        # EpisodicMemory / RelationshipMemory).  The persona-direct
        # recall path bypasses the MemoryStore facade, so the tier
        # must resolve its own active session for ``sessions=None`` to
        # be correct on that path.
        self._active_session_id = resolve_session_id_silent()
        # ISSUE-0081 PR 3 — tenant snapshot (call-time scope wins on use).
        self._active_principal_id = resolve_principal_id_silent()
        # ISSUE-0085 PR 3 — epoch (run/test) snapshot, same shape.
        self._active_epoch_id = resolve_epoch_id_silent()

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
        protection_level: str = PROTECTION_LEVEL_DEFAULT,
        source_channel_id: str | None = None,
    ) -> str:
        """Persist a new fact tuple.  Returns the generated ``fact_id``.

        ``protection_level`` / ``source_channel_id`` (RFC 0037 §C — v16,
        PR 3) persist VERBATIM — rule-(a) normalization is owned by the
        persona-side stamp site (the close-consolidation extractor via
        ``normalize_for_stamp``), which memory must not import.  Omitted
        (direct/test/operator callers) → the ``internal`` default, so no
        path writes a fact without a protection level; a mislabeled row
        fails closed at read time (§A rule (c)).  Supersession restamps
        by construction — a superseding assertion is a NEW row stamped
        from its own source (§C item 3) — and reinforcement never
        touches the column.

        Enforces RFC 0026 §F **symmetric latest-asserted-wins**: only
        one live row per ``(agent_id, subject, predicate)`` survives,
        and the row with the greatest ``asserted_at`` wins.  Out-of-order
        and equal-timestamp writes resolve deterministically — see
        :mod:`agents.memory._facts_supersede` for the chain rule and
        :class:`tests.unit.python.test_fact_store_supersede.TestSymmetricLatestAssertedWins`
        for the pinned cases.

        Predicate validation runs through the injected validator
        (PR 2 wires the enumerated allowlist).

        Subject canonicalisation (PR 5c — PR #341 review L-2)
        -----------------------------------------------------
        The production write path (PR 2 extractor) canonicalises
        before calling here, but three classes of direct caller
        bypass that discipline — test fixtures, operator-seeded
        facts (RFC 0026 OQ #9), and the future RFC 0013 erasure
        backfill.  Without canonicalisation at this layer they
        silently write rows under non-canonical subjects and miss
        the recall path that PR 3 wired to ``_subject_seeds →
        canonicalize_subject``, defeating the MT-MEMORY-005
        dementia-test invariant.  The storage primitive is now
        authoritative — every persisted row carries the canonical
        subject regardless of caller discipline.
        """
        # Cheap value checks first — surfacing "subject must not be
        # empty" or a certainty-range error before the (potentially
        # PR 2 allowlist-backed) predicate validator means a caller
        # that violates two preconditions sees the more obviously-wrong
        # one first.
        if not subject or not subject.strip():
            raise ValueError("subject must not be empty")
        if not 0.0 <= certainty <= 1.0:
            raise ValueError(
                f"certainty must be in [0.0, 1.0], got {certainty}",
            )
        self._predicate_validator(predicate)
        # Topic-amendment blast-radius bound: object length + RFC 0009
        # delimiter escape, enforced at the storage boundary so every
        # write path (extractor, operator-seeded, fixtures) is covered.
        validate_object(object)
        # Canonicalise after the empty-check so the ValueError text
        # stays familiar; ``canonicalize_subject`` is idempotent so
        # the production write path (extractor pre-canonicalises) is
        # unaffected.
        subject = canonicalize_subject(subject)
        # Normalise session_id at the storage boundary (RFC 0031 Phase 2
        # PR 4 — PR 1 F16 carry-forward).  Symmetric with the other three
        # persona-memory tier write paths.
        session_id = normalize_session_id(session_id)
        # ISSUE-0081 PR 3: active tenant for the row tag + supersede chain key.
        principal_id = resolve_active_principal(self._active_principal_id)
        # ISSUE-0085 PR 3: active epoch for the row tag + supersede chain key.
        epoch_id = resolve_active_epoch(self._active_epoch_id)

        db = self._ensure_db()
        fact_id = str(uuid.uuid4())

        await db.execute(
            """
            INSERT INTO facts
                (fact_id, agent_id, subject, predicate, object,
                 certainty, source_interaction_id, asserted_at,
                 last_recalled_at, superseded_by, session_id, principal_id,
                 epoch_id, protection_level, source_channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
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
                principal_id,
                epoch_id,
                protection_level,
                source_channel_id,
            ),
        )

        result = await _apply_supersession(
            db,
            agent_id=self._agent_id,
            subject=subject,
            predicate=predicate,
            asserted_at=asserted_at,
            new_fact_id=fact_id,
            session_id=session_id,
            principal_id=principal_id,
            epoch_id=epoch_id,
        )
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
                n_superseded = len(result.superseded_older_ids) + (
                    1 if result.self_superseded_by else 0
                )
                if n_superseded:
                    inst.facts_superseded.add(
                        n_superseded, attributes={"agent.id": self._agent_id},
                    )

        # RFC 0026 §G audit emission — after commit so the log cannot
        # record a write that did not happen.
        _emit_audit(
            "fact.store", agent_id=self._agent_id, fact_id=fact_id,
            subject=subject, predicate=predicate, object=object,
            source_interaction_id=source_interaction_id,
        )
        for older_id in result.superseded_older_ids:
            _emit_audit(
                "fact.supersede", agent_id=self._agent_id,
                superseded_fact_id=older_id, by_fact_id=fact_id,
            )
        if result.self_superseded_by is not None:
            _emit_audit(
                "fact.supersede", agent_id=self._agent_id,
                superseded_fact_id=fact_id,
                by_fact_id=result.self_superseded_by,
            )
        return fact_id

    # ─── Read path ─────────────────────────────────────────

    async def recall(
        self,
        *,
        subject: str,
        limit: int = 10,
        include_superseded: bool = False,
        sessions: list[str] | str | None = None,
    ) -> list[Fact]:
        """Return facts about ``subject`` ordered most-recent-first.

        Filters by ``agent_id`` so cross-agent leakage is impossible
        (RFC 0008 §H ACL).  ``superseded_by IS NOT NULL`` rows are
        excluded by default — PR 4's retraction policy reuses this
        filter without further changes.

        ``limit`` is clamped to ``_MAX_RECALL_LIMIT`` (100).  PR 3 will
        compose this with the ``MemoryBudget`` allocator's per-tier
        token slice; this floor is the hard upper bound on row count.

        Subject canonicalisation (PR 5c — PR #341 review L-2):
        symmetric with :meth:`store`, a non-canonical query subject
        (``"Bob "`` from a fixture that bypasses the runtime's
        ``_subject_seeds`` pre-step) canonicalises before the SELECT.
        Empty / whitespace-only subjects raise ``ValueError`` first,
        symmetric with :meth:`store`'s explicit empty-check.

        ``sessions`` (RFC 0031 Phase 2 PR 3): four-mode §D filter — see
        :func:`agents.memory._session_filter._resolve_session_list`.
        Default ``None`` → active session + ``legacy`` carve-out (the
        load-bearing dementia-test surface; pre-RFC fact rows persist
        with ``session_id='legacy'`` and stay visible after upgrade).
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        limit = min(limit, _MAX_RECALL_LIMIT)
        subject = canonicalize_subject(subject)
        session_list = _resolve_session_list(
            sessions, self._active_session_id,
        )
        sess_clause, sess_params = session_in_clause(
            session_list, column="session_id",
        )
        # ISSUE-0081 PR 3 — strict tenant equality appended to every branch.
        princ_clause, princ_params = principal_eq_clause(
            resolve_active_principal(self._active_principal_id),
            column="principal_id",
        )
        # ISSUE-0085 PR 3 — strict epoch equality, same shape.
        epoch_clause, epoch_params = epoch_eq_clause(
            resolve_active_epoch(self._active_epoch_id),
            column="epoch_id",
        )

        db = self._ensure_db()
        # Deterministic order (RFC 0044 golden-trace portability): `asserted_at
        # DESC` alone leaves ties SQLite-implementation-defined, so two facts
        # asserted in the same instant — e.g. within one interaction under the eval
        # driver's FrozenClock — could recall in a host-dependent order and shift
        # the assembled prompt (and its request hash / length-derived token count)
        # between the record host and CI. `rowid` (insertion order) is the tiebreak:
        # it is identical across record and replay, which INSERT the same facts in
        # the same order, and stays monotonic under the store's ops (inserts append,
        # supersede/delete never renumber survivors, nothing VACUUMs). It is not
        # selected, only sorted on. `fact_id` is a random uuid4 (see `store`), so it
        # is NOT a portable tiebreak.
        if include_superseded:
            sql = (
                f"SELECT {_FACT_SELECT} FROM facts "
                "WHERE agent_id = ? AND subject = ?"
                f"{sess_clause}{princ_clause}{epoch_clause} "
                "ORDER BY asserted_at DESC, rowid DESC LIMIT ?"
            )
        else:
            sql = (
                f"SELECT {_FACT_SELECT} FROM facts "
                "WHERE agent_id = ? AND subject = ? "
                "AND superseded_by IS NULL"
                f"{sess_clause}{princ_clause}{epoch_clause} "
                "ORDER BY asserted_at DESC, rowid DESC LIMIT ?"
            )
        async with db.execute(
            sql, (
                self._agent_id, subject, *sess_params, *princ_params,
                *epoch_params, limit,
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    async def mark_recalled(self, fact_ids: Iterable[str], *, at: float | None = None) -> None:
        # RFC 0026 PR 4 — see :mod:`._facts_reinforce` for §G rationale.
        await _mark_recalled_for_agent(self._ensure_db(), self._agent_id, fact_ids, at=at)

    async def topic_subjects(self, *, limit: int = 200) -> list[str]:
        """Distinct live ``topic.*`` subjects, most-recent-first.

        The recall-seeding enumeration (RFC 0026 topic amendment /
        RFC 0049 P1) — scoped exactly like :meth:`recall` (agent +
        §D-default sessions + principal + epoch) so PR 1 widens
        capture without pre-widening scope.  See
        :mod:`agents.memory._facts_topics` for the SQL body.
        """
        return await _topic_subjects_for_agent(
            self._ensure_db(),
            agent_id=self._agent_id,
            limit=min(limit, _MAX_RECALL_LIMIT * 2),
            session_list=_resolve_session_list(
                None, self._active_session_id,
            ),
            principal_id=resolve_active_principal(
                self._active_principal_id,
            ),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

    # ─── Retraction / cleanup ──────────────────────────────

    async def supersede(self, fact_id: str, by_fact_id: str) -> bool:
        """Manually mark ``fact_id`` as superseded by ``by_fact_id``.

        Storage-only retract for callers (PR 4 + future RFC 0027
        consolidation) that retract a fact without writing a successor of
        identical ``(subject, predicate)``; the latest-asserted-wins policy
        in :meth:`store` is the common path.  Principal-scoped via
        :func:`._facts_supersede.retract_fact` (ISSUE-0081 PR 3 review) so
        it is symmetric with the automatic chain — a tenant cannot retract
        another tenant's fact by id.  Returns ``True`` iff a row was updated.
        """
        return await _retract_fact(
            self._ensure_db(), agent_id=self._agent_id,
            fact_id=fact_id, by_fact_id=by_fact_id,
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
        )

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

        Delegates to :func:`agents.memory._facts_erasure.delete_by_subject`
        for the SQL body; see that helper for the disjoint-bucket
        return-shape contract and the ``source_interaction_id``
        reverse-edge traversal rationale.
        """
        return await _delete_by_subject(
            self._ensure_db(), self._agent_id, subject_id,
        )

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
            # RFC 0037 §C (migration v16): surfaced for the §D gate.
            protection_level=row[11],
            source_channel_id=row[12],
        )
