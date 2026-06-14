"""
Callable migration handlers extracted from :mod:`agents.memory.migrations`.

Each handler covers a migration that needs imperative Python rather
than a single ``executescript`` block — typically because the version
rewrites tables with the 12-step ALTER TABLE pattern, performs an
``ALTER TABLE ... ADD COLUMN`` that predates SQLite's
``IF NOT EXISTS`` (< 3.35), or guards against partial-restore
baselines where the migration must inspect the live schema before
issuing DDL.  See each handler's docstring for per-version rationale.

The registry currently covers ``v4`` through ``v13``.  Migration ``v8``
(RFC 0026 PR 1 — declarative-facts table) lives in
:mod:`agents.memory._migration_facts`, migration ``v9`` (RFC 0031
Phase 2 PR 1 — ``session_id`` on notes) lives in
:mod:`agents.memory._migration_notes_session`, migration ``v10``
(RFC 0031 Phase 2 PR 5 — ``session_id`` on interactions) lives in
:mod:`agents.memory._migration_interactions_session`, migration
``v11`` (ISSUE-0081 PR 3 — ``principal_id`` on all five tiers) lives in
:mod:`agents.memory._migration_principal`, migration ``v12``
(ISSUE-0085 PR 2 — ``epoch_id`` on all five tiers) lives in
:mod:`agents.memory._migration_epoch`, and migration ``v13`` (RFC 0031
amendment — F-7 Option D, ISSUE-0093: ``identity`` column on
relationships) lives in :mod:`agents.memory._migration_identity`; all
are re-exported below so this module stays under the 500-line repo-wide
soft cap.  The split mirrors :mod:`agents.observability._metrics_facts`.

Public API (``_MIGRATION_HANDLERS`` plus the ``_apply_migration_N``
callables) is re-exported by :mod:`agents.memory.migrations` for
backwards compatibility — call sites and tests should keep importing
from the migrations module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiosqlite

# Migration v8 (RFC 0026 PR 1) lives in :mod:`agents.memory._migration_facts`
# and v9 (RFC 0031 Phase 2 PR 1) in
# :mod:`agents.memory._migration_notes_session` so this module stays under
# the 500-line cap — mirrors the :mod:`agents.observability._metrics_facts`
# split.  Re-exported here so existing call sites
# (``from ._migration_handlers import _apply_migration_8``) continue to
# work without churn.
from ._migration_epoch import _apply_migration_12
from ._migration_facts import _apply_migration_8
from ._migration_governance_id import _apply_migration_15
from ._migration_identity import _apply_migration_13
from ._migration_identity_backfill import _apply_migration_14
from ._migration_interactions_session import _apply_migration_10
from ._migration_notes_session import _apply_migration_9
from ._migration_principal import _apply_migration_11


async def _apply_migration_4(db: aiosqlite.Connection) -> None:
    """Rebuild relationships/interactions with participant_type columns.

    Uses the `12-step ALTER TABLE`_ pattern in a single transaction.
    Also creates the ``users`` table so the schema is consistent even
    when ``UserStore`` is not used.

    Existing data is backfilled with ``participant_type = 'agent'`` and
    ``other_participant_type = 'agent'``.

    .. _12-step ALTER TABLE:
       https://www.sqlite.org/lang_altertable.html#otheralter
    """
    # -- 1. Users table (idempotent) --
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            participant_id   TEXT PRIMARY KEY,
            display_name     TEXT NOT NULL,
            participant_type TEXT NOT NULL DEFAULT 'user',
            created_at       REAL NOT NULL,
            last_seen_at     REAL NOT NULL
        )
        """
    )

    # -- 2. Rebuild relationships: 12-step ALTER TABLE --
    # Crash recovery: if a previous run crashed between DROP TABLE
    # relationships and ALTER TABLE RENAME, the staging table exists
    # but the final name does not.  Complete the interrupted rename to
    # avoid data loss.  (PR #120 review F-3: migration crash-recovery gap.)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='relationships_new'"
    )
    if await cursor.fetchone():
        cursor2 = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='relationships'"
        )
        if not await cursor2.fetchone():
            await db.execute(
                "ALTER TABLE relationships_new RENAME TO relationships"
            )
        else:
            # Both tables exist — drop the staging table (incomplete copy).
            await db.execute("DROP TABLE relationships_new")

    # Check if migration already partially ran (crash recovery).
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='relationships'"
    )
    old_exists = await cursor.fetchone()

    if old_exists:
        # Check if already migrated (has participant_id column).
        cursor = await db.execute("PRAGMA table_info(relationships)")
        columns = {row[1] for row in await cursor.fetchall()}

        if "participant_id" not in columns:
            # Step 1: Create new table
            await db.execute(
                """
                CREATE TABLE relationships_new (
                    participant_id TEXT NOT NULL,
                    participant_type TEXT NOT NULL DEFAULT 'agent',
                    other_participant_id TEXT NOT NULL,
                    other_participant_type TEXT NOT NULL DEFAULT 'agent',
                    trust_score REAL DEFAULT 0.5,
                    interaction_count INTEGER DEFAULT 0,
                    last_interaction_at REAL,
                    notes TEXT,
                    PRIMARY KEY (participant_id, participant_type,
                                 other_participant_id, other_participant_type)
                )
                """
            )
            # Step 2: Copy data with backfill
            await db.execute(
                """
                INSERT INTO relationships_new
                    (participant_id, participant_type,
                     other_participant_id, other_participant_type,
                     trust_score, interaction_count, last_interaction_at, notes)
                SELECT agent_id, 'agent',
                       other_agent_id, 'agent',
                       trust_score, interaction_count, last_interaction_at, notes
                FROM relationships
                """
            )
            # Step 3: Drop old table
            await db.execute("DROP TABLE relationships")
            # Step 4: Rename new table
            await db.execute(
                "ALTER TABLE relationships_new RENAME TO relationships"
            )
    else:
        # Fresh DB or relationships was already dropped — create directly.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS relationships (
                participant_id TEXT NOT NULL,
                participant_type TEXT NOT NULL DEFAULT 'agent',
                other_participant_id TEXT NOT NULL,
                other_participant_type TEXT NOT NULL DEFAULT 'agent',
                trust_score REAL DEFAULT 0.5,
                interaction_count INTEGER DEFAULT 0,
                last_interaction_at REAL,
                notes TEXT,
                PRIMARY KEY (participant_id, participant_type,
                             other_participant_id, other_participant_type)
            )
            """
        )

    # Recreate indexes with new column names.
    await db.execute("DROP INDEX IF EXISTS idx_relationships_agent")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_relationships_participant "
        "ON relationships(participant_id, participant_type)"
    )

    # -- 3. Rebuild interactions: 12-step ALTER TABLE --
    # Crash recovery for interactions_new (same pattern as relationships).
    # (PR #120 review F-3.)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='interactions_new'"
    )
    if await cursor.fetchone():
        cursor2 = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='interactions'"
        )
        if not await cursor2.fetchone():
            await db.execute(
                "ALTER TABLE interactions_new RENAME TO interactions"
            )
        else:
            await db.execute("DROP TABLE interactions_new")

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='interactions'"
    )
    old_exists = await cursor.fetchone()

    if old_exists:
        cursor = await db.execute("PRAGMA table_info(interactions)")
        columns = {row[1] for row in await cursor.fetchall()}

        if "participant_id" not in columns:
            await db.execute(
                """
                CREATE TABLE interactions_new (
                    id TEXT PRIMARY KEY,
                    participant_id TEXT NOT NULL,
                    participant_type TEXT NOT NULL DEFAULT 'agent',
                    other_participant_id TEXT NOT NULL,
                    other_participant_type TEXT NOT NULL DEFAULT 'agent',
                    interaction_type TEXT NOT NULL,
                    outcome TEXT,
                    sentiment REAL DEFAULT 0.0,
                    created_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                """
                INSERT INTO interactions_new
                    (id, participant_id, participant_type,
                     other_participant_id, other_participant_type,
                     interaction_type, outcome, sentiment, created_at)
                SELECT id, agent_id, 'agent',
                       other_agent_id, 'agent',
                       interaction_type, outcome, sentiment, created_at
                FROM interactions
                """
            )
            await db.execute("DROP TABLE interactions")
            await db.execute(
                "ALTER TABLE interactions_new RENAME TO interactions"
            )
    else:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL,
                participant_type TEXT NOT NULL DEFAULT 'agent',
                other_participant_id TEXT NOT NULL,
                other_participant_type TEXT NOT NULL DEFAULT 'agent',
                interaction_type TEXT NOT NULL,
                outcome TEXT,
                sentiment REAL DEFAULT 0.0,
                created_at REAL NOT NULL
            )
            """
        )

    # Recreate composite covering index with new column names.
    await db.execute("DROP INDEX IF EXISTS idx_interactions_lookup")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_interactions_participant_lookup "
        "ON interactions(participant_id, participant_type, "
        "other_participant_id, other_participant_type, created_at DESC)"
    )

    # NOTE: this commit makes the DDL changes durable, but the version
    # record in schema_version is written by _apply_migrations() AFTER
    # this function returns.  If the process crashes between this commit
    # and the version INSERT, the migration re-runs on restart.  The
    # crash-recovery guards above (PRAGMA table_info + relationships_new
    # check) make the re-run safe by detecting the already-migrated schema.
    # Removing this commit would not help because SQLite DDL (CREATE TABLE,
    # DROP TABLE) causes implicit commits in many modes.
    # (PR #120 review F-7: migration version recording atomicity.)
    await db.commit()


async def _apply_migration_5(db: aiosqlite.Connection) -> None:
    """RFC 0020 §D: extend ``episodes`` with interaction lifecycle columns.

    Adds four nullable columns and an ``(scope, closed_at)`` index to
    support per-interaction episode rows.  Existing rows keep ``NULL`` in
    every new column — recall code treats those as legacy single-turn
    episodes per RFC 0020 §I.

    Idempotency: ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS``
    in SQLite < 3.35 (Persatrix does not yet require that minimum), so the
    column existence is checked via ``PRAGMA table_info`` first.  This
    keeps the migration safe on a partial-failure replay before
    ``schema_version`` is updated.

    The ``CREATE INDEX IF NOT EXISTS`` call is belt-and-suspenders.
    """
    # Defensive guard: if a partial migration baseline (e.g. tests that
    # pre-record schema_version 1–4 without creating the episodes table)
    # leaves no `episodes` table, ALTER TABLE would crash.  PRAGMA on a
    # nonexistent table returns no rows; treat that as "nothing to do" so
    # the migration is safe in mixed states.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='episodes'"
    )
    if not await cursor.fetchone():
        # No DDL ran, so there is nothing to commit.  The version record
        # in schema_version is written by _apply_migrations() AFTER this
        # function returns (parity with _apply_migration_4's tail-commit
        # contract), so the no-op return is the only side-effect.
        return

    # Discover existing column names exactly once.
    cursor = await db.execute("PRAGMA table_info(episodes)")
    existing = {row[1] for row in await cursor.fetchall()}

    # New columns introduced by RFC 0020 §D — order matches the RFC.
    new_columns: tuple[tuple[str, str], ...] = (
        ("interaction_id", "TEXT"),
        ("started_at", "REAL"),
        ("closed_at", "REAL"),
        ("turn_count", "INTEGER"),
        ("scope", "TEXT"),
    )
    for name, sqltype in new_columns:
        if name not in existing:
            await db.execute(
                f"ALTER TABLE episodes ADD COLUMN {name} {sqltype}"
            )

    # Index supports per-scope recall queries (RFC 0020 §G).
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_scope "
        "ON episodes(scope, closed_at)"
    )

    await db.commit()


async def _apply_migration_6(db: aiosqlite.Connection) -> None:
    """RFC 0008 PR plan PR 5: confidence + last_validated_at on episodes.

    Adds two columns used by the procedural-tier confidence decay path:

    - ``confidence REAL NOT NULL DEFAULT 1.0`` — stored ``c_0`` value
      at the last validation event.  ``DEFAULT 1.0`` lets legacy
      episodic rows (and PR 2 procedural rows that only carried the
      mapped ``importance`` column) round-trip cleanly without a
      backfill — the read path treats DEFAULT 1.0 as "fully fresh"
      which is the historical pre-decay behaviour.
    - ``last_validated_at REAL`` — wall-clock seconds of the last
      ``refresh_confidence`` call (``NULL`` for never-validated rows;
      the read path falls back to ``created_at`` in that case).

    The ``confidence`` column is intentionally separate from
    ``importance`` so the eviction hybrid score (which consumes
    ``importance``) and the procedural decay clock (which consumes
    ``confidence``) can evolve independently — see RFC 0008 §G.

    Idempotency: same ``PRAGMA table_info`` guard as v5 because
    ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS`` in SQLite
    < 3.35 (Persatrix does not require that minimum).
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='episodes'"
    )
    if not await cursor.fetchone():
        # No episodes table yet — nothing to alter.  Mirrors the v5
        # defensive guard for partial-baseline test fixtures.  No DDL
        # ran, so there is nothing to commit; the version record is
        # written by _apply_migrations() AFTER this function returns.
        return

    cursor = await db.execute("PRAGMA table_info(episodes)")
    existing = {row[1] for row in await cursor.fetchall()}

    new_columns: tuple[tuple[str, str], ...] = (
        # ``confidence`` lands NOT NULL with DEFAULT 1.0 so legacy
        # episodic rows (and PR 2's procedural rows that only used the
        # ``importance`` column) upgrade cleanly without a backfill
        # script — the read path treats DEFAULT 1.0 as "fully fresh"
        # which matches the historical (pre-decay) behaviour.
        ("confidence", "REAL NOT NULL DEFAULT 1.0"),
        # ``last_validated_at`` stays nullable; the read-time decay path
        # falls back to ``created_at`` when this column is NULL so the
        # decay clock starts from the row's birth for never-validated
        # procedures.
        ("last_validated_at", "REAL"),
    )
    for name, sqltype in new_columns:
        if name not in existing:
            await db.execute(
                f"ALTER TABLE episodes ADD COLUMN {name} {sqltype}"
            )

    await db.commit()


async def _apply_migration_7(db: aiosqlite.Connection) -> None:
    """RFC 0031 Phase 1: ``session_id`` on episodes + relationships.

    Adds ``session_id TEXT NOT NULL DEFAULT 'legacy'`` to both tables and
    creates per-table indexes (``idx_episodes_session``,
    ``idx_rel_session``) so Phase 2 per-session recall has a column +
    index pair to filter on without a follow-up migration.

    The ``'legacy'`` default is the synthetic carve-out described by RFC
    0031 OQ #2 — Phase 3 CLI's ``persatrix session new --label legacy``
    is rejected so the identifier can never collide with an
    operator-created session.  Mirrors the orchestrator-side
    ``channels.DefaultSessionID`` value pinned in PR 2.

    Each table is handled independently with the same ``sqlite_master``
    guard pattern used by v5/v6 — the ``ALTER TABLE ... ADD COLUMN``
    statement is not idempotent before SQLite 3.35, so the
    ``PRAGMA table_info`` check prevents a partial-restore baseline
    (``schema_version`` recorded up to v6 but one or both of the target
    tables missing) from crashing the migration.
    """
    # Episodes half.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='episodes'",
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(episodes)")
        existing = {row[1] for row in await cursor.fetchall()}
        if "session_id" not in existing:
            await db.execute(
                "ALTER TABLE episodes ADD COLUMN "
                "session_id TEXT NOT NULL DEFAULT 'legacy'",
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_session "
            "ON episodes(session_id)",
        )

    # Relationships half — independent guard so a baseline that has one
    # table but not the other (an unusual but legal partial-restore
    # shape, mirrored from v5/v6's contract) still progresses cleanly.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='relationships'",
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(relationships)")
        existing = {row[1] for row in await cursor.fetchall()}
        if "session_id" not in existing:
            await db.execute(
                "ALTER TABLE relationships ADD COLUMN "
                "session_id TEXT NOT NULL DEFAULT 'legacy'",
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rel_session "
            "ON relationships(session_id)",
        )

    await db.commit()


# Explicit dict replaces the previous globals().get() dispatch, which was
# fragile (typo in handler name silently fell through) and not IDE-friendly
# (Find Usages / refactoring didn't discover the dynamic lookup).
# (PR 6 review fix: PR 2 finding #1.)
_MIGRATION_HANDLERS: dict[int, Callable[[aiosqlite.Connection], Awaitable[None]]] = {
    4: _apply_migration_4,
    5: _apply_migration_5,
    6: _apply_migration_6,
    7: _apply_migration_7,
    8: _apply_migration_8,
    9: _apply_migration_9,
    10: _apply_migration_10,
    11: _apply_migration_11,
    12: _apply_migration_12,
    13: _apply_migration_13,
    14: _apply_migration_14,
    15: _apply_migration_15,
}
