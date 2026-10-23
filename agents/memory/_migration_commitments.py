"""RFC 0021 Phase 2 PR 1 migration v20 — the ``commitments`` table.

A persona that says "I'll send you the notes on Friday" keeps nothing but
the sentence today.  This table is where that promise will be kept — and,
in the same rows, the instructions other members of an organization give
the persona (RFC 0012 §E's directives).  Nothing reads it yet: the
``CommitmentStore`` of the Phase 2 plan's PR 2 is its first reader, and
the same plan's PR 3, the directive calls RFC 0012 uses, its second.

**One table, two kinds** (the plan's H-2).  ``kind`` tells them apart:

* a ``commitment`` is the persona's own promise — it has a ``due_at``
  and a ``state`` (RFC 0021 §F's lifecycle), and no directive status or
  issuer;
* a ``directive`` is someone's instruction to the persona — it has a
  ``directive_status`` (RFC 0012's seven, kept outside the commitment
  ``state`` vocabulary) and an issuer, no ``state``, and its ``due_at``
  is optional.

The ``commitments_kind_shape`` check keeps those four columns apart: a
reader that filters on ``kind`` never meets a commitment without a due
time and a state, or a directive without a status and a non-empty
issuer.  Column checks keep the description non-empty and both times
numbers — a due time stored as text would sort after every real one and
never fall due.  Only RFC 0021's own ``state`` has a vocabulary check.
The RFC 0012 columns (the directive status, channel and issuer
authority, scope, the structured form, the graph version, the conflict
fields) carry none, beyond ``conflict_failures`` never going below zero,
and only the status and issuer are kept off a commitment row: their
vocabularies are RFC 0012's and RFC 0028's, checked by PR 3's store
calls, and a SQLite check can only change by rebuilding the table — the
directive statuses have already grown once.

**Scoped like a fact** (H-3).  ``session_id``, ``principal_id``,
``epoch_id`` and ``protection_level`` are declared exactly as the facts
tier's are, defaults included, so PR 2 reads the table with that tier's
filters.  No ``source_channel_id``: a commitment is authored in a turn,
not taken from a channel, and a directive's channel is its
``issuing_channel_id``.  Timestamps are epoch seconds, as RFC 0021 §F's
schema has them.

**Indexes.**  ``agent_id`` leads each, as every reader filters on it (the
v19 rationale); then the open-commitments block's read (kind, state, due
time — nearest due first), RFC 0012's read (kind, directive status), and
``list_commitments_to`` (kind, target party, state).  ``kind`` is in the
last so it outranks the other two for that read, rather than winning a
tie by the order the indexes were made in.

**Guards.**  The ``sqlite_master`` guard every sibling handler has: a
partial restore that already holds a ``commitments`` table keeps it, and
an index is created only when its columns are present.  Such a table is
logged as a warning when it lacks any column, since v20 is recorded over
it and no later run looks again.  Upgrading a v19 store, no transaction
is open, so SQLite commits each ``CREATE`` on its own and a crash after
the table and before its indexes leaves an unindexed table (on a fresh
store the umbrella's open transaction carries them together); the
replay's ``CREATE INDEX IF NOT EXISTS`` loop runs whether or not this
call made the table, which repairs it.  The tail commit matches the
siblings'.
"""

from __future__ import annotations

import logging
import re

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ..session_id import LEGACY_SESSION_ID
from ._migration_protection import PROTECTION_LEVEL_DEFAULT

__all__ = ["COMMITMENT_STATES", "DIRECTIVE_STATUSES", "_apply_migration_20"]

logger = logging.getLogger(__name__)

#: RFC 0021 §F's lifecycle.  Nothing moves a row to ``missed`` before
#: Phase 3's janitor, but the value is the RFC's, so it is allowed now.
COMMITMENT_STATES: tuple[str, ...] = ("open", "fulfilled", "missed", "cancelled")

#: RFC 0012's directive statuses, as its PR plan asks of row A2.  PR 3's
#: store calls check them, not the table, so a new one needs no rebuild.
#: Which may follow which is RFC 0012's rule, enforced in its PRs 9–10a.
DIRECTIVE_STATUSES: tuple[str, ...] = (
    "pending",
    "accepted",
    "adapted",
    "refused",
    "escalated",
    "superseded",
    "fulfilled",
)

_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("idx_commitments_due", ("agent_id", "kind", "state", "due_at")),
    ("idx_commitments_directive", ("agent_id", "kind", "directive_status")),
    ("idx_commitments_target", ("agent_id", "kind", "target_party", "state")),
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# Every interpolated value is a module constant above, never input.
_CREATE_TABLE = f"""
CREATE TABLE commitments (
    -- SQLite lets a TEXT primary key hold NULL unless it says NOT NULL.
    id                    TEXT PRIMARY KEY NOT NULL,
    agent_id              TEXT NOT NULL,
    kind                  TEXT NOT NULL
        CONSTRAINT commitments_kind
        CHECK (kind IN ('commitment', 'directive')),
    description           TEXT NOT NULL
        CONSTRAINT commitments_description
        CHECK (length(description) > 0),
    -- Epoch seconds; text that is not a number never falls due.
    due_at                REAL
        CONSTRAINT commitments_due_at
        CHECK (typeof(due_at) IN ('real', 'null')),
    target_party          TEXT,
    created_at            REAL NOT NULL
        CONSTRAINT commitments_created_at
        CHECK (typeof(created_at) = 'real'),
    state                 TEXT
        CONSTRAINT commitments_state
        CHECK (state IN ({_in(COMMITMENT_STATES)})),
    fulfilled_at          REAL,
    source_interaction_id TEXT,
    tags_json             TEXT NOT NULL DEFAULT '[]',
    issuer_id             TEXT,
    issuing_channel_id    TEXT,
    channel_authority     TEXT,
    issuer_authority      TEXT,
    scope                 TEXT,
    directive_status      TEXT,
    polarity              TEXT,
    action_class          TEXT,
    target                TEXT,
    graph_version         INTEGER,
    conflicts_with_json   TEXT NOT NULL DEFAULT '[]',
    conflict_checked_at   REAL,
    conflict_failures     INTEGER NOT NULL DEFAULT 0
        CONSTRAINT commitments_conflict_failures
        CHECK (conflict_failures >= 0),
    session_id            TEXT NOT NULL DEFAULT '{LEGACY_SESSION_ID}',
    principal_id          TEXT NOT NULL DEFAULT '{DEFAULT_PRINCIPAL_ID}',
    epoch_id              TEXT NOT NULL DEFAULT '{DEFAULT_EPOCH_ID}',
    protection_level      TEXT NOT NULL DEFAULT '{PROTECTION_LEVEL_DEFAULT}',
    CONSTRAINT commitments_kind_shape CHECK (
        (kind = 'commitment'
            AND due_at IS NOT NULL AND state IS NOT NULL
            AND directive_status IS NULL AND issuer_id IS NULL)
        -- IS NOT NULL stays: length(NULL) is NULL, which a CHECK passes.
        OR (kind = 'directive'
            AND directive_status IS NOT NULL
            AND issuer_id IS NOT NULL AND length(issuer_id) > 0
            AND state IS NULL)
    )
)
"""  # noqa: S608 — only module constants are interpolated.

#: The column names, read from the statement above: each column line is
#: indented four spaces and starts lower-case, unlike its constraints.
_COLUMNS: frozenset[str] = frozenset(
    re.findall(r"^ {4}([a-z_]+) ", _CREATE_TABLE, re.MULTILINE),
)


async def _apply_migration_20(db: aiosqlite.Connection) -> None:
    """Create the ``commitments`` table and its three indexes."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='commitments'",
    )
    if await cursor.fetchone() is None:
        await db.execute(_CREATE_TABLE)

    cursor = await db.execute("PRAGMA table_info(commitments)")
    columns = {row[1] for row in await cursor.fetchall()}
    if missing := _COLUMNS - columns:
        logger.warning(
            "migration v20: the commitments table already present lacks "
            "%s; it is kept as it is and v20 recorded, so the commitments "
            "tier cannot use it until it is rebuilt",
            ", ".join(sorted(missing)),
        )
    for name, indexed in _INDEXES:
        # A stub table from a partial restore may lack the columns, and
        # CREATE INDEX on a missing column raises.
        if set(indexed) <= columns:
            await db.execute(
                f"CREATE INDEX IF NOT EXISTS {name} "
                f"ON commitments({', '.join(indexed)})",
            )

    await db.commit()
