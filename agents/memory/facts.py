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

import logging
from collections.abc import Callable, Collection, Iterable

import aiosqlite

from ..epoch_id import resolve_epoch_id_silent
from ..principal_id import resolve_principal_id_silent
from ..session_id import normalize_session_id, resolve_session_id_silent
from ._epoch_filter import epoch_eq_clause, resolve_active_epoch
from ._facts_erasure import delete_by_subject as _delete_by_subject
from ._facts_reinforce import mark_recalled_for_agent as _mark_recalled_for_agent
from ._facts_supersede import retract_fact as _retract_fact
from ._facts_topics import predicate_in_clause
from ._facts_topics import topic_subjects_for_agent as _topic_subjects_for_agent
from ._facts_write import insert_fact
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from ._principal_filter import principal_eq_clause, resolve_active_principal
from ._session_filter import _resolve_session_list, session_in_clause
from .fact_predicates import (
    canonicalize_subject,
    validate_predicate,
)
from .fact_types import _FACT_COLS, _FACT_SELECT, Fact, row_to_fact
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
        speaker_id: str | None = None,
    ) -> str:
        """Persist a new fact tuple.  Returns the generated ``fact_id``.

        Delegates to :func:`agents.memory._facts_write.insert_fact` for
        validation, the INSERT, the RFC 0026 §F supersession pass and the
        §G audit emission — see that helper for every one of those
        contracts, and for what ``speaker_id`` (ISSUE-0131, migration 18)
        and ``protection_level`` / ``source_channel_id`` (RFC 0037 §C)
        mean on the row.

        What stays here is what only the tier knows: the three ambient
        axes.  ``session_id`` normalises at this storage boundary
        (RFC 0031 Phase 2 PR 4), and the active tenant and epoch resolve
        through the same :func:`resolve_active_principal` /
        :func:`resolve_active_epoch` seams the recall path uses — so a
        row is always readable by the principal that wrote it, and the
        supersede chain keys on the same tuple recall filters on.
        """
        return await insert_fact(
            self._ensure_db(),
            self._agent_id,
            subject=subject,
            predicate=predicate,
            object=object,
            source_interaction_id=source_interaction_id,
            asserted_at=asserted_at,
            certainty=certainty,
            # RFC 0031 Phase 2 PR 4 (PR 1 F16 carry-forward) — symmetric
            # with the other three persona-memory tier write paths.
            session_id=normalize_session_id(session_id),
            # ISSUE-0081 PR 3 / ISSUE-0085 PR 3: the row tag AND the
            # supersede chain key.
            principal_id=resolve_active_principal(self._active_principal_id),
            epoch_id=resolve_active_epoch(self._active_epoch_id),
            predicate_validator=self._predicate_validator,
            protection_level=protection_level,
            source_channel_id=source_channel_id,
            speaker_id=speaker_id,
        )

    # ─── Read path ─────────────────────────────────────────

    async def recall(
        self,
        *,
        subject: str,
        limit: int = 10,
        include_superseded: bool = False,
        sessions: list[str] | str | None = None,
        predicates: Collection[str] | None = None,
    ) -> list[Fact]:
        """Return facts about ``subject`` ordered most-recent-first.

        ``predicates`` (RFC 0026 topic amendment) narrows to a
        predicate class; ``None`` = every class, the person-seed
        default.  The topic-seed path passes ``TOPIC_PREDICATES`` —
        the seed set is what bounds which subjects a stimulus can
        read, so one induced ``("bob", "topic.owned_by", …)`` tuple
        must not turn ``bob`` into a key for every fact about Bob.

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
        pred_clause, pred_params = predicate_in_clause(predicates)

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
                f"{pred_clause}{sess_clause}{princ_clause}{epoch_clause} "
                "ORDER BY asserted_at DESC, rowid DESC LIMIT ?"
            )
        else:
            sql = (
                f"SELECT {_FACT_SELECT} FROM facts "
                "WHERE agent_id = ? AND subject = ? "
                "AND superseded_by IS NULL"
                f"{pred_clause}{sess_clause}{princ_clause}{epoch_clause} "
                "ORDER BY asserted_at DESC, rowid DESC LIMIT ?"
            )
        async with db.execute(
            sql, (
                self._agent_id, subject, *pred_params, *sess_params,
                *princ_params, *epoch_params, limit,
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row_to_fact(row) for row in rows]

    async def mark_recalled(self, fact_ids: Iterable[str], *, at: float | None = None) -> None:
        # RFC 0026 PR 4 — see :mod:`._facts_reinforce` for §G rationale.
        await _mark_recalled_for_agent(self._ensure_db(), self._agent_id, fact_ids, at=at)

    async def topic_subjects(
        self,
        *,
        limit: int = 200,
        sessions: list[str] | str | None = None,
    ) -> list[str]:
        """Distinct live ``topic.*`` subjects, most-recent-first.

        The recall-seeding enumeration (RFC 0026 topic amendment /
        RFC 0049 P1) — scoped exactly like :meth:`recall`, adding no
        scope of its own.  ``sessions`` takes the same four-mode §D
        shape as :meth:`recall`; the L2 cross-room SHADOW pass (RFC
        0049 PR 2) passes ``"*"`` so a topic taught in another room can
        seed the widened read — epoch/principal equality still applies
        unconditionally.  See :mod:`agents.memory._facts_topics`.
        """
        return await _topic_subjects_for_agent(
            self._ensure_db(),
            agent_id=self._agent_id,
            limit=min(limit, _MAX_RECALL_LIMIT * 2),
            session_list=_resolve_session_list(
                sessions, self._active_session_id,
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
        for the SQL body, and resolves the active principal through the
        same :func:`resolve_active_principal` seam the recall and write
        paths use, so a caller erases exactly the rows it could read
        (ISSUE-0081 residual).  See that helper for the disjoint-bucket
        return shape, the ``source_interaction_id`` reverse edge, and why
        the tenant axis is scoped while session / epoch are not.
        """
        return await _delete_by_subject(
            self._ensure_db(),
            self._agent_id,
            subject_id,
            principal_id=resolve_active_principal(self._active_principal_id),
        )
