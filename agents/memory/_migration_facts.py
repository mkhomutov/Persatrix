"""RFC 0026 PR 1 migration v8 — declarative-facts table + indexes.

Split out of :mod:`agents.memory._migration_handlers` so the parent
module stays under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  The split mirrors the
:mod:`agents.observability._metrics_facts` separation — one RFC-scoped
helper, re-exported by the parent module for backwards compatibility.

Lives outside :mod:`agents.memory.facts` because migrations run from
:func:`agents.memory.migrations._apply_migrations` before any
``FactStore`` exists; the handler must be importable from the migration
umbrella without a circular import on the facts CRUD module.
"""

from __future__ import annotations

import aiosqlite


async def _apply_migration_8(db: aiosqlite.Connection) -> None:
    """RFC 0026 PR 1: declarative-facts tier — new ``facts`` table.

    Creates the per-agent fact-tuple store that the extractor wired in
    RFC 0026 PR 2 will write to.  Schema is additive — no ``episodes``
    / ``relationships`` / ``notes`` rewrites; PR 1 ships the table and
    two supporting indexes only.

    Columns mirror the RFC 0026 §A dataclass shape:

    - ``fact_id``: ``uuid4`` string (RFC names ULID; the codebase uses
      ``uuid.uuid4`` everywhere else — ``episodes.id``, ``notes.id``,
      ``interactions.id``).  Either is fine as a PK; matching the
      surrounding convention beats introducing a one-tier ULID
      dependency.
    - ``agent_id`` + ``subject``: covered by ``idx_facts_subject_agent``
      so RFC 0026 §D recall (``SELECT … WHERE agent_id=? AND subject=?``)
      is an index scan.
    - ``session_id TEXT NOT NULL DEFAULT 'legacy'``: matches the
      RFC 0031 v7 contract on ``episodes`` / ``relationships`` so PR 1
      writes through the same operator-namespace plumbing without a
      follow-up migration.  ``idx_facts_session`` mirrors the per-table
      session index introduced for the other tiers.
    - ``superseded_by``: nullable self-reference.  PR 4 will use this
      for latest-asserted-wins retraction; PR 1 lands the column so the
      retraction policy is a recall-side filter change, not a schema
      bump.

    Idempotency: ``CREATE TABLE`` is gated on the ``facts`` table not
    yet existing, mirroring the v5/v6/v7 ``sqlite_master`` guard
    pattern.  This protects a partial-restore baseline where
    ``schema_version`` recorded up to v7 but a previous v8 attempt
    half-applied (or a previous fresh-DB initialisation pre-created a
    stub ``facts`` table).  Index creation uses ``IF NOT EXISTS`` as
    belt-and-suspenders on the replay path.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='facts'",
    )
    table_existed = await cursor.fetchone() is not None
    if not table_existed:
        await db.execute(
            """
            CREATE TABLE facts (
                fact_id               TEXT PRIMARY KEY,
                agent_id              TEXT NOT NULL,
                subject               TEXT NOT NULL,
                predicate             TEXT NOT NULL,
                object                TEXT NOT NULL,
                certainty             REAL NOT NULL DEFAULT 1.0,
                source_interaction_id TEXT,
                asserted_at           REAL NOT NULL,
                last_recalled_at      REAL,
                superseded_by         TEXT,
                session_id            TEXT NOT NULL DEFAULT 'legacy'
            )
            """,
        )
        # Indexes only created when this handler created the table — a
        # pre-existing stub table (partial-restore baseline) does not
        # carry the v8 schema and ``CREATE INDEX ON facts(agent_id, …)``
        # would fail.  Index creation on the replay path (table already
        # has the v8 schema because the previous handler call succeeded)
        # is covered by the ``CREATE INDEX IF NOT EXISTS`` branch below.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_subject_agent "
            "ON facts(agent_id, subject)",
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_session "
            "ON facts(session_id)",
        )
    else:
        # Replay path — handler was called twice on the same DB (crash
        # between DDL and version-record).  Re-create indexes
        # idempotently *only* if the canonical ``agent_id`` /
        # ``session_id`` columns are present, so a stub partial-restore
        # baseline does not raise.
        cursor = await db.execute("PRAGMA table_info(facts)")
        cols = {row[1] for row in await cursor.fetchall()}
        if {"agent_id", "subject"}.issubset(cols):
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_subject_agent "
                "ON facts(agent_id, subject)",
            )
        if "session_id" in cols:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_session "
                "ON facts(session_id)",
            )

    await db.commit()
