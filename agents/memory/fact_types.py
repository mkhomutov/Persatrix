"""Data model + column constants for the declarative-fact tier (RFC 0026).

Split out of :mod:`agents.memory.facts` so the parent module stays under
the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the
:mod:`agents.memory.relationship_types` / :mod:`agents.memory.store_types`
precedent — the dataclass + SELECT-column constants live beside the tier
they describe but out of the CRUD body.  :class:`~agents.memory.facts.FactStore`
re-exports :class:`Fact` so ``from agents.memory.facts import Fact`` keeps
working for every existing call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ._migration_protection import PROTECTION_LEVEL_DEFAULT

__all__ = ["Fact", "_FACT_COLS", "_FACT_SELECT", "row_to_fact"]


@dataclass(frozen=True)
class Fact:
    """A single declarative-fact tuple — RFC 0026 §A data shape.

    Frozen so a recall caller cannot silently desynchronise its in-memory
    view from the persisted row.  Mutations must round-trip through
    :class:`~agents.memory.facts.FactStore` (``store`` / ``supersede``).

    ``fact_id`` is a ``uuid4`` hex string in this implementation; the
    RFC text names ULID but the rest of the storage layer
    (``Episode.id``, ``Note.id``, ``Interaction.id``) uses
    ``uuid.uuid4`` and the ``asserted_at`` column already gives
    chronological ordering, so the ULID time-prefix property is not
    load-bearing here.

    ``source_interaction_id`` is typed ``str | None`` and the DDL
    column is nullable.  PR 5a amended RFC §A to permit ``NULL`` —
    three legitimate callers (test fixtures, the future RFC 0013
    erasure backfill, and the OQ #9 operator-seeded fact path) commit
    rows without a source interaction; the PR 2 extractor always
    populates it on the production write path.

    ``asserted_at`` is ``float`` (epoch seconds) rather than the RFC's
    ``datetime`` — matches the codebase convention (``episodes.created_at``,
    ``interactions.started_at`` are both REAL epoch-seconds) so a single
    conversion seam (``time.time()``) covers every tier.  Tracked as a
    RFC §A amendment in PR 6.

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
    # RFC 0037 §C/§D (migration v16, PR 4): protection columns projected
    # onto recall so the §D injection gate can rank each candidate fact
    # (and name its channel in the rule-(c) log).  Defaults match the v16
    # column DEFAULT so hand-built fixtures round-trip; a corrupted stored
    # label fails closed at the gate (rule (c)), never here.
    protection_level: str = PROTECTION_LEVEL_DEFAULT
    source_channel_id: str | None = None


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
    # RFC 0037 §C (v16): surfaced to the §D gate.
    "protection_level",
    "source_channel_id",
)
_FACT_SELECT = ", ".join(_FACT_COLS)


def row_to_fact(row: Sequence[Any]) -> Fact:
    """Build a :class:`Fact` from a ``_FACT_SELECT`` row.

    Lives beside :data:`_FACT_COLS` (rather than on ``FactStore``) so
    the column order and the field mapping cannot drift apart across a
    module boundary — and so :mod:`agents.memory.facts` stays under the
    500-line review cap.
    """
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
