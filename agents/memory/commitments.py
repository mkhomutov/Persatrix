"""The commitments tier (RFC 0021 Phase 2 PR 2).

A persona that says "I'll send you the notes on Friday" used to keep
nothing but the sentence.  :class:`CommitmentStore` keeps the promise: what
was promised, by when, and to whom, in the ``commitments`` table that
memory-store migration v20 made.  This PR adds the store only; the prompt
block that shows open commitments (PR 4) and the tools a persona makes
them with (PR 5) come later, so nothing calls it yet.

**The lifecycle** is RFC 0021 §F's.  A commitment is made ``open`` and
moves once, to ``fulfilled`` or ``cancelled``; after that it is read-only.
``missed`` is Phase 3's, set by a janitor that does not exist yet, so no
call here moves a row there — an open commitment past its due time stays
open, and the reads list it first.

**Scoped like a fact** (the Phase 2 plan's H-3).  Each row is stamped with
the session, person (principal) and epoch of the turn that made it, and
every call — reads, and the updates that close a row — sees only this
persona's commitment rows in the turn's scope: the room's session plus the
``legacy`` carve-out, and exactly the turn's person and epoch.  So another
person's commitment, another epoch's, or one of RFC 0012's directive rows
in the same table is "not found", never "refused".  Commitments stay in
their room: unlike facts, no call reads across sessions, because a
commitment carries no source channel for the audience check to judge.

**What is stored** (H-6, H-7).  A description is capped at
:data:`MAX_DESCRIPTION_CHARS` and passed through the sanitizer as the
persona's own output before it is stored; a flag is logged, not refused,
as for channel text.  The target party gets the same checks, since the
prompt block shows it on the same line.  A persona has at most
:data:`MAX_OPEN_COMMITMENTS` open in an epoch, counted across people and
rooms, so it cannot pass the cap by spreading its promises out (H-7).
One person's open commitments therefore count against everyone's; the
maintainer kept that on the Phase 2 plan's PR 2 review (F-1).  Another
epoch's do not count, since this one can neither read nor close them,
and nor does one past its due time (F-19): Phase 3's janitor will mark
it ``missed``, which does not count, and until then a turn that cannot
see it could never release it.  The protection level is stored as the caller gives it: the persona
stamps it, since memory must not import the classification rules.

**Time** comes only from the :class:`~agents.clock.Clock` the store is
given, agent time by default — never the real clock
(``test_agent_time.py`` scans this package for it).
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

from ..clock import AgentClock, Clock
from ..epoch_id import resolve_epoch_id_silent
from ..principal_id import resolve_principal_id_silent
from ..security import CONTEXT_SOURCE_AGENT_OUTPUT, sanitize
from ..session_id import normalize_session_id, resolve_session_id_silent
from ._epoch_filter import epoch_eq_clause, resolve_active_epoch
from ._migration_commitments import COMMITMENT_STATES
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from ._principal_filter import principal_eq_clause, resolve_active_principal
from ._salience import (
    COMMITMENTS_APPEND_SALIENCE,
    emit_for_tier,
    emit_session_write,
)
from ._session_filter import (
    _resolve_session_list,
    resolve_active_session,
    session_in_clause,
)
from ._write_boundary import execute_write
from .migrations import _apply_migrations

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "MAX_OPEN_COMMITMENTS",
    "Commitment",
    "CommitmentCapError",
    "CommitmentClosedError",
    "CommitmentNotFoundError",
    "CommitmentState",
    "CommitmentStore",
]

logger = logging.getLogger(__name__)

#: RFC 0021 §Security: a description (or target party) longer than this
#: is refused.
MAX_DESCRIPTION_CHARS = 1_000

#: RFC 0021 §Security's soft cap, per persona and epoch (H-7), on open
#: commitments not yet due.
MAX_OPEN_COMMITMENTS = 500

# The most rows one read returns, as the facts and notes tiers bound theirs.
_MAX_LIST_LIMIT = 100

#: The values of :data:`COMMITMENT_STATES`, as a type.
CommitmentState = Literal["open", "fulfilled", "missed", "cancelled"]


@dataclass(frozen=True, slots=True)
class Commitment:
    """One of the persona's own promises — RFC 0021 §F's data shape.

    Frozen: a change goes through :class:`CommitmentStore`.  The id is a
    ``uuid4`` string, as every other tier's is, not the RFC's ULID: the
    due and creation times already order the rows.  ``fulfilled_at`` is set
    only on a fulfilled row.  ``session_id`` and ``protection_level`` are
    what the prompt block (PR 4) needs to place and gate the row.
    """

    id: str
    description: str
    due_at: float
    target_party: str | None
    created_at: float
    state: CommitmentState
    fulfilled_at: float | None
    source_interaction_id: str | None
    tags: tuple[str, ...]
    session_id: str
    protection_level: str


_SELECT = (
    "id, description, due_at, target_party, created_at, state, fulfilled_at, "
    "source_interaction_id, tags_json, session_id, protection_level"
)


def _row_to_commitment(row: Sequence[Any]) -> Commitment:
    return Commitment(
        id=row[0],
        description=row[1],
        due_at=row[2],
        target_party=row[3],
        created_at=row[4],
        state=row[5],
        fulfilled_at=row[6],
        source_interaction_id=row[7],
        tags=tuple(json.loads(row[8])),
        session_id=row[9],
        protection_level=row[10],
    )


class CommitmentNotFoundError(LookupError):
    """No commitment with this id is visible in the turn's scope."""


class CommitmentClosedError(ValueError):
    """The commitment is no longer open, so it cannot change."""

    def __init__(self, commitment_id: str, state: str) -> None:
        # Both arguments go to ``args``, so the error copies and pickles.
        super().__init__(commitment_id, state)
        self.commitment_id = commitment_id
        self.state = state

    def __str__(self) -> str:
        return (
            f"commitment {self.commitment_id} is already {self.state}; "
            "a closed commitment cannot change"
        )


class CommitmentCapError(ValueError):
    """The persona already has :data:`MAX_OPEN_COMMITMENTS` open."""


# Making one: what the RFC asks of each field, before anything is stored.


def _check_text(value: str, field: str) -> str:
    """Strip ``value`` and refuse it over :data:`MAX_DESCRIPTION_CHARS`."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text; got {type(value).__name__}")
    text = value.strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"{field} is {len(text)} characters; a commitment's is at "
            f"most {MAX_DESCRIPTION_CHARS} characters",
        )
    return text


def _check_description(description: str) -> str:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be non-empty text")
    return _check_text(description, "description")


def _check_due_at(due_at: float) -> float:
    # ``bool`` is an ``int``; ``True`` as a due time is a caller's bug.
    if isinstance(due_at, bool) or not isinstance(due_at, (int, float)):
        raise TypeError(
            f"due_at must be epoch seconds; got {type(due_at).__name__}",
        )
    # SQLite stores NaN as NULL, and infinity sorts after every real time
    # and never falls due.  An int past a float's range is as infinite.
    try:
        value = float(due_at)
    except OverflowError:
        value = math.inf
    if not math.isfinite(value):
        raise ValueError(f"due_at must be a finite time; got {value!r}")
    return value


def _check_tags(tags: Sequence[str]) -> list[str]:
    if isinstance(tags, str):  # would be stored one character per tag
        raise TypeError("tags must be a list of strings, not one string")
    checked = list(tags)
    if not all(isinstance(tag, str) for tag in checked):
        raise TypeError("tags must all be strings")
    return checked


# One statement, so two calls at once cannot both pass the count.  The
# count's epoch predicate comes from the shared helper; a row past its due
# time is left out of it, as a ``missed`` one will be.
_INSERT_UNDER_CAP = """
INSERT INTO commitments
    (id, agent_id, kind, description, due_at, target_party, created_at,
     state, source_interaction_id, tags_json, session_id, principal_id,
     epoch_id, protection_level)
SELECT ?, ?, 'commitment', ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?
WHERE (
    SELECT COUNT(*) FROM commitments
    WHERE agent_id = ? AND kind = 'commitment' AND state = 'open'
        AND due_at >= ?{epoch}
) < ?
"""


class CommitmentStore:
    """Per-persona make, list, fulfil and cancel over ``commitments``.

    Opened as the facts tier is (:class:`~agents.memory.facts.FactStore`):
    its own ``aiosqlite`` connection against ``db_path``, or a caller's
    ``shared_db``, with the migrations run at :meth:`initialize`.
    """

    def __init__(
        self,
        agent_id: str,
        db_path: str = "data/memory.db",
        *,
        shared_db: aiosqlite.Connection | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = shared_db
        self._owns_db = shared_db is None
        self._clock: Clock = clock if clock is not None else AgentClock()
        # The scope the store was built in — only the fallback: a turn's
        # bound session, person and epoch win on every call.
        self._active_session_id = resolve_session_id_silent()
        self._active_principal_id = resolve_principal_id_silent()
        self._active_epoch_id = resolve_epoch_id_silent()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def initialize(self) -> None:
        """Open the database (when not sharing) and run the migrations."""
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
                "CommitmentStore not initialised — call initialize() first",
            )
        return self._db

    def _scope(self) -> tuple[str, list[Any]]:
        """The ``WHERE`` predicate every call reads and closes with:
        this persona's commitment rows in the turn's scope."""
        sess, sess_params = session_in_clause(
            _resolve_session_list(None, self._active_session_id),
            column="session_id",
        )
        princ, princ_params = principal_eq_clause(
            resolve_active_principal(self._active_principal_id),
            column="principal_id",
        )
        epoch, epoch_params = epoch_eq_clause(
            resolve_active_epoch(self._active_epoch_id), column="epoch_id",
        )
        return (
            f"agent_id = ? AND kind = 'commitment'{sess}{princ}{epoch}",
            [self._agent_id, *sess_params, *princ_params, *epoch_params],
        )

    # ─── Making one ────────────────────────────────────────

    async def create(
        self,
        *,
        description: str,
        due_at: float,
        target_party: str | None = None,
        source_interaction_id: str | None = None,
        tags: Sequence[str] = (),
        protection_level: str = PROTECTION_LEVEL_DEFAULT,
    ) -> str:
        """Make an open commitment and return its id.

        ``due_at`` is epoch seconds; parsing the model's time strings is
        the tool's (PR 5), and so is refusing a time already past.  A blank
        ``target_party`` is stored as none.  Raises :class:`ValueError` or
        :class:`TypeError` for a field the RFC refuses, and
        :class:`CommitmentCapError` when the persona already has
        :data:`MAX_OPEN_COMMITMENTS` open and not yet due — the error says
        so, and nothing is stored.
        """
        db = self._ensure_db()
        text = _check_description(description)
        due = _check_due_at(due_at)
        tag_list = _check_tags(tags)
        target = (
            _check_text(target_party, "target_party")
            if target_party is not None else ""
        )

        description_scan = sanitize(text, source=CONTEXT_SOURCE_AGENT_OUTPUT)
        target_scan = (
            sanitize(target, source=CONTEXT_SOURCE_AGENT_OUTPUT) if target else None
        )
        principal_id = resolve_active_principal(self._active_principal_id)
        epoch_id = resolve_active_epoch(self._active_epoch_id)
        session_id = normalize_session_id(
            resolve_active_session(self._active_session_id),
        )
        epoch, epoch_params = epoch_eq_clause(epoch_id, column="epoch_id")
        commitment_id = str(uuid.uuid4())
        now = float(self._clock.now())
        cursor = await execute_write(
            db, _INSERT_UNDER_CAP.format(epoch=epoch),
            (
                commitment_id, self._agent_id, description_scan.content,
                due, target_scan.content if target_scan else None,
                now, source_interaction_id,
                json.dumps(tag_list), session_id, principal_id, epoch_id,
                protection_level,
                self._agent_id, now, *epoch_params,
                MAX_OPEN_COMMITMENTS,
            ),
        )
        if cursor.rowcount != 1:
            # The rows it counts are mostly ones this turn cannot see.
            raise CommitmentCapError(
                f"already {MAX_OPEN_COMMITMENTS} open commitments not yet "
                "due, counted across every person and room; another can be "
                "made once one falls due, or is fulfilled or cancelled where "
                "it was made",
            )
        for field, scan in (
            ("description", description_scan), ("target_party", target_scan),
        ):
            if scan is not None and scan.flagged:
                logger.warning(
                    "commitment %s of %s stored with sanitizer flags %s in its %s",
                    commitment_id, self._agent_id, ",".join(scan.flags), field,
                )
        self._count_write(session_id, now)
        return commitment_id

    def _count_write(self, session_id: str, written_at: float) -> None:
        """The write counter and bus event every persona-memory tier sends,
        the event stamped by the store's clock."""
        emit_session_write(
            agent_id=self._agent_id, session_id=session_id, surface="commitment",
        )
        emit_for_tier(
            agent_id=self._agent_id,
            tier="commitments",
            salience=COMMITMENTS_APPEND_SALIENCE,
            written_at=written_at,
        )

    # ─── Reading ───────────────────────────────────────────

    async def get(self, commitment_id: str) -> Commitment | None:
        """The commitment with this id, in any state, or ``None`` when
        there is none in the turn's scope."""
        where, params = self._scope()
        async with self._ensure_db().execute(
            f"SELECT {_SELECT} FROM commitments WHERE id = ? AND {where}",
            (commitment_id, *params),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _row_to_commitment(row)

    async def list_commitments(
        self,
        *,
        state: str | None = "open",
        within_seconds: float | None = None,
        limit: int = 20,
    ) -> list[Commitment]:
        """Commitments in ``state`` (``None``: every state), nearest due
        first, so an overdue one leads.

        ``within_seconds`` keeps those due no later than that many seconds
        from now, the overdue included; ``None`` keeps all.  Rows due at
        the same time keep the order they were made in, so a prompt built
        from them is the same on every host.  ``limit`` is at most 100.
        """
        return await self._select(
            state=state, within_seconds=within_seconds, limit=limit,
        )

    async def list_open_commitments(
        self, within_seconds: float | None = None, *, limit: int = 20,
    ) -> list[Commitment]:
        """RFC 0021 §F's read for the prompt block: open commitments due
        within ``within_seconds`` (``None``: all), nearest due first."""
        return await self._select(
            state="open", within_seconds=within_seconds, limit=limit,
        )

    async def list_commitments_to(
        self,
        target_party: str,
        *,
        state: str | None = "open",
        limit: int = 20,
    ) -> list[Commitment]:
        """RFC 0021 §F's "what did I promise Alice?": commitments to
        ``target_party`` in ``state`` (``None``: every state), nearest
        due first.  The name matches whatever the case of its ASCII
        letters (SQLite's ``NOCASE``), since the model writes it freely."""
        target = _check_text(target_party, "target_party")
        if not target:
            raise ValueError("target_party must be non-empty")
        return await self._select(
            state=state, within_seconds=None, limit=limit, target=target,
        )

    async def _select(
        self,
        *,
        state: str | None,
        within_seconds: float | None,
        limit: int,
        target: str | None = None,
    ) -> list[Commitment]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if state is not None and state not in COMMITMENT_STATES:
            raise ValueError(
                f"state must be one of {COMMITMENT_STATES} or None, got {state!r}",
            )
        where, params = self._scope()
        if state is not None:
            where += " AND state = ?"
            params.append(state)
        if target is not None:
            where += " AND target_party = ? COLLATE NOCASE"
            params.append(target)
        if within_seconds is not None:
            if not within_seconds >= 0:  # also refuses NaN
                raise ValueError(
                    f"within_seconds must be >= 0, got {within_seconds}",
                )
            where += " AND due_at <= ?"
            params.append(self._clock.now() + within_seconds)
        # ``rowid`` breaks a tie in insertion order, the same on every host
        # (the facts tier's reason): the ids are random.
        async with self._ensure_db().execute(
            f"SELECT {_SELECT} FROM commitments WHERE {where} "
            "ORDER BY due_at ASC, rowid ASC LIMIT ?",
            (*params, min(limit, _MAX_LIST_LIMIT)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_commitment(row) for row in rows]

    # ─── Closing one ───────────────────────────────────────

    async def fulfill(self, commitment_id: str) -> Commitment:
        """Mark an open commitment fulfilled, now, and return it.

        Raises :class:`CommitmentNotFoundError` when the turn's scope holds
        no such commitment, and :class:`CommitmentClosedError` when it is
        no longer open.
        """
        return await self._close(commitment_id, "fulfilled")

    async def cancel(self, commitment_id: str) -> Commitment:
        """Cancel an open commitment and return it; raises as
        :meth:`fulfill` does."""
        return await self._close(commitment_id, "cancelled")

    async def _close(
        self, commitment_id: str, state: CommitmentState,
    ) -> Commitment:
        db = self._ensure_db()
        where, params = self._scope()
        now = float(self._clock.now())
        fulfilled_at = now if state == "fulfilled" else None
        # The update decides; the read after it only says why it did not.
        # Nothing reopens a row, so the read cannot disagree with it.
        cursor = await execute_write(
            db,
            "UPDATE commitments SET state = ?, fulfilled_at = ? "
            f"WHERE id = ? AND state = 'open' AND {where}",
            (state, fulfilled_at, commitment_id, *params),
        )
        commitment = await self.get(commitment_id)
        if commitment is None:
            raise CommitmentNotFoundError(f"no commitment {commitment_id!r}")
        if cursor.rowcount != 1:
            raise CommitmentClosedError(commitment_id, commitment.state)
        self._count_write(commitment.session_id, now)
        return commitment
