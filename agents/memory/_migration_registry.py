"""The forward-only schema-migration registry (``MIGRATIONS``).

Extracted from :mod:`agents.memory.migrations` so that module's *logic*
(``_apply_migrations`` + the shared scoring fragments) stays honestly under
the 500-line code cap.  This list is reference data whose length scales with
**migration history** — one entry per schema version, each carrying the prose
that explains why that version exists — not with authored logic, exactly the
"size scales with data, not prose" split already made for
``scripts/checks/file_size_allowlist.py`` and
:mod:`agents.memory._migration_handlers`.  Re-exported by
:mod:`agents.memory.migrations`, so every existing
``from .migrations import MIGRATIONS`` call site is unchanged.

Adding a version: append a ``(version, description, sql)`` tuple here.  An
empty ``sql`` means the work lives in a callable handler registered in
``_MIGRATION_HANDLERS`` (:mod:`agents.memory._migration_handlers`) — the path
every non-idempotent ``ALTER TABLE`` and every data transform takes.
"""

from __future__ import annotations

__all__ = ["MIGRATIONS"]


# ─── Schema migrations ─────────────────────────────────────

# Forward-only migrations: (version, description, SQL).
# Each migration's SQL may contain multiple statements separated by ";".
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "Initial schema: episodes + agent_state + FTS5",
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            context_json TEXT,
            outcome TEXT,
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            last_accessed_at REAL,
            tags_json TEXT,
            created_at REAL NOT NULL,
            compressed_at REAL,
            compression_level INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_agent
            ON episodes(agent_id);
        CREATE INDEX IF NOT EXISTS idx_episodes_importance
            ON episodes(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_created
            ON episodes(created_at DESC);

        CREATE TABLE IF NOT EXISTS agent_state (
            agent_id TEXT PRIMARY KEY,
            interaction_count INTEGER DEFAULT 0,
            persona_state_json TEXT,
            updated_at REAL NOT NULL
        );
        """,
    ),
    (
        2,
        "Notes table, FTS5 index, and sync triggers",
        """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT,
            access_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_notes_agent
            ON notes(agent_id);
        CREATE INDEX IF NOT EXISTS idx_notes_topic
            ON notes(agent_id, topic);
        CREATE INDEX IF NOT EXISTS idx_notes_created
            ON notes(created_at DESC);
        """,
    ),
    (
        3,
        "Relationships and interactions tables",
        """
        CREATE TABLE IF NOT EXISTS relationships (
            agent_id TEXT NOT NULL,
            other_agent_id TEXT NOT NULL,
            trust_score REAL DEFAULT 0.5,
            interaction_count INTEGER DEFAULT 0,
            last_interaction_at REAL,
            notes TEXT,
            PRIMARY KEY (agent_id, other_agent_id)
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            other_agent_id TEXT NOT NULL,
            interaction_type TEXT NOT NULL,
            outcome TEXT,
            sentiment REAL DEFAULT 0.0,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_relationships_agent
            ON relationships(agent_id);
        -- Composite covering index for get_relationship_summary() query:
        -- WHERE agent_id=? AND other_agent_id=? ORDER BY created_at DESC LIMIT N
        -- Replaces separate agent and created_at indexes; the composite
        -- index satisfies both the WHERE filter and ORDER BY in a single
        -- index scan, avoiding a temp sort.
        CREATE INDEX IF NOT EXISTS idx_interactions_lookup
            ON interactions(agent_id, other_agent_id, created_at DESC);
        """,
    ),
    # Migration 4 uses the callable path (_apply_migration_4) because it
    # rebuilds tables with a new composite PK — this is NOT idempotent
    # and requires a manually managed transaction.  The SQL field is empty;
    # _apply_migrations() detects the callable and invokes it directly.
    (
        4,
        "Generalize relationships/interactions to participant pairs; add users table",
        "",  # handled by _apply_migration_4()
    ),
    # Migration 5 (RFC 0020 §D) extends `episodes` with interaction
    # columns via ALTER TABLE ADD COLUMN.  SQLite's `ADD COLUMN` is not
    # idempotent before 3.35 (no `IF NOT EXISTS`), so this uses the
    # callable path with PRAGMA table_info() guards for crash-recovery
    # safety.  See docs/rfcs/0020-interaction-lifecycle.md §D for the
    # column rationale.
    (
        5,
        "RFC 0020: episodes interaction columns + scope index",
        "",  # handled by _apply_migration_5()
    ),
    # Migration 6 (RFC 0008 PR plan PR 5) adds the procedural-tier
    # ``confidence`` and ``last_validated_at`` columns to the
    # ``episodes`` table.  Same callable-handler rationale as v5: the
    # ``ALTER TABLE ... ADD COLUMN`` path is not idempotent before
    # SQLite 3.35 so the handler does the ``PRAGMA table_info`` guard.
    (
        6,
        "RFC 0008 PR 5: procedural-tier confidence + last_validated_at",
        "",  # handled by _apply_migration_6()
    ),
    # Migration 7 (RFC 0031 Phase 1) tags ``episodes`` and ``relationships``
    # with the operator-namespace ``session_id`` column.  Same callable-
    # handler rationale as v5/v6: ``ALTER TABLE ... ADD COLUMN`` is not
    # idempotent before SQLite 3.35 so each half guards with
    # ``PRAGMA table_info`` and the missing-table partial-restore shape
    # short-circuits cleanly.  See docs/rfcs/0031-pr-plan.md PR 3 for
    # the column / index contract.
    (
        7,
        "RFC 0031: session_id on episodes + relationships",
        "",  # handled by _apply_migration_7()
    ),
    # Migration 8 (RFC 0026 PR 1) creates the new declarative-facts
    # ``facts`` table — schema is additive, no rewrites of existing
    # tables.  Lives on the callable path because the handler skips the
    # CREATE when a stub ``facts`` table is already present (partial-
    # restore baseline shape, mirrors the v5/v6/v7 ``sqlite_master``
    # guard).  See docs/rfcs/0026-pr-plan.md PR 1 for the column
    # contract + the RFC 0013 erasure-traversal rationale.
    (
        8,
        "RFC 0026: declarative-facts table + subject/session indexes",
        "",  # handled by _apply_migration_8()
    ),
    # Migration 9 (RFC 0031 Phase 2 PR 1) tags the ``notes`` tier with
    # the operator-namespace ``session_id`` column — the last
    # persona-memory recall surface missing a session dimension after
    # v7 (episodes/relationships) and v8 (facts).  Same callable-handler
    # rationale as v5/v6/v7: ``ALTER TABLE ... ADD COLUMN`` is not
    # idempotent before SQLite 3.35 so the handler guards with
    # ``PRAGMA table_info`` and short-circuits cleanly when a
    # partial-restore baseline has no ``notes`` table.  See
    # docs/rfcs/0031-phase2-pr-plan.md PR 1 for the column / index
    # contract.
    (
        9,
        "RFC 0031 Phase 2: session_id on notes",
        "",  # handled by _apply_migration_9()
    ),
    # Migration 10 (RFC 0031 Phase 2 PR 5) tags the ``interactions``
    # table with the operator-namespace ``session_id`` column.  Migration
    # v7 added ``session_id`` to the parent ``relationships`` row but
    # not to ``interactions`` — the secondary fetch in
    # ``get_relationship_summary`` leaked cross-session interaction
    # history into the persona prompt (ISSUE-0080).  Same callable-
    # handler rationale as v7/v8/v9 — ``ALTER TABLE ... ADD COLUMN``
    # is not idempotent before SQLite 3.35.
    (
        10,
        "RFC 0031 Phase 2: session_id on interactions",
        "",  # handled by _apply_migration_10()
    ),
    # Migration 11 (ISSUE-0081 PR 3) adds the tenant/principal dimension
    # ``principal_id`` to all five persona-memory tables in one version —
    # ``episodes`` / ``relationships`` / ``facts`` / ``notes`` /
    # ``interactions``.  Where ``session_id`` (v7–v10) scopes by operator
    # run, ``principal_id`` scopes by tenant with a STRICT-equality recall
    # predicate (no carve-out), closing the cross-tenant leak ISSUE-0081
    # flagged.  The four UUID-keyed tiers gain it as a column; the
    # participant-tuple-keyed ``relationships`` table is rebuilt with
    # ``principal_id`` *in the primary key* so a second tenant's upsert
    # cannot mutate the first tenant's aggregate row (review H2).  Same
    # callable-handler rationale as v7/v9/v10 — ``ALTER TABLE ... ADD
    # COLUMN`` is not idempotent before SQLite 3.35.
    # See docs/rfcs/0031-per-session-namespacing-channels.md §C amendment.
    (
        11,
        "ISSUE-0081: principal_id on all five persona-memory tiers",
        "",  # handled by _apply_migration_11()
    ),
    # Migration 12 (ISSUE-0085 PR 2) adds the run/test-isolation dimension
    # ``epoch_id`` to all five persona-memory tables in one version —
    # ``episodes`` / ``relationships`` / ``facts`` / ``notes`` /
    # ``interactions``.  Where ``principal_id`` (v11) scopes by tenant,
    # ``epoch_id`` scopes by test run / logical branch with the SAME
    # STRICT-equality recall predicate (no carve-out, no ``'*'`` sentinel) —
    # the structural half of the F-3 fix.  The four UUID-keyed tiers gain it
    # as a column; the participant-tuple-keyed ``relationships`` table is
    # rebuilt with ``epoch_id`` *in the primary key* (alongside the
    # ``principal_id`` v11 put there) so a rerun's upsert under a fresh epoch
    # cannot mutate the prior run's aggregate row.  Same callable-handler
    # rationale as v7/v9/v10/v11 — ``ALTER TABLE ... ADD COLUMN`` is not
    # idempotent before SQLite 3.35.  See
    # docs/issues/ISSUE-0085-epoch-axis-run-isolation.md.
    (
        12,
        "ISSUE-0085: epoch_id on all five persona-memory tiers",
        "",  # handled by _apply_migration_12()
    ),
    # Migration 13 (RFC 0031 amendment — F-7 Option D, ISSUE-0093) adds a
    # nullable ``identity TEXT`` (JSON) column to ``relationships`` so person
    # identity (name / role / stable preferences) lives on the genuinely
    # cross-room relationship tier (PK omits ``session_id``) instead of being
    # retrofitted onto room-scoped ``contact:*`` notes by the F-7 Option-A
    # recall carve-out.  Unlike v11/v12 this is **not** a table rebuild —
    # ``identity`` is per-row payload, not a key column — so it uses the
    # simple v7 ``ADD COLUMN`` skeleton with the same ``sqlite_master`` +
    # ``PRAGMA table_info`` partial-restore / crash-replay guards.  Lives in
    # :mod:`agents.memory._migration_identity`.  See
    # docs/rfcs/0031-amendment-person-identity-cross-room-tier.md (PR D1).
    (
        13,
        "RFC 0031 amendment: identity column on relationships (F-7 Option D)",
        "",  # handled by _apply_migration_13()
    ),
    # Migration 14 (RFC 0031 amendment — F-7 Option D, ISSUE-0093, PR D4) is
    # the one-time **data backfill** that follows v13's schema: it reads
    # pre-cutover ``contact:<id>`` notes and merges their parsed identity
    # onto the matching ``relationships`` row, so personas don't lose
    # identity learned before D2/D3 moved it onto the cross-room tier.  A
    # data transform (not DDL) — so it lives in a callable handler that
    # reads/writes both tables with individual ``db.execute`` calls + one
    # tail commit, never ``executescript`` (whose implicit COMMIT the
    # ``_apply_migrations`` note warns off for non-idempotent transforms).
    # It resolves the one relationship-PK axis a note does not record —
    # ``other_participant_type`` — by inheriting it from existing rows, or
    # defaulting an orphan to ``"agent"``.  Lives in
    # :mod:`agents.memory._migration_identity_backfill`.  See
    # docs/rfcs/0031-amendment-person-identity-cross-room-tier.md (PR D4).
    (
        14,
        "RFC 0031 amendment: backfill contact notes to relationship identity "
        "(F-7 Option D)",
        "",  # handled by _apply_migration_14()
    ),
    # Migration 15 (ISSUE-0102 PR 2) promotes the RFC 0030 governance
    # interaction id from the episode ``context_json`` blob (where PR 1 stamped
    # it for display) to a queryable ``governance_interaction_id`` column on
    # ``episodes``, then backfills the column from each row's context so a
    # PR-1-shaped row becomes look-up-able.  This lets the closed-interaction
    # read filter match the channel-side id (``interaction_id = ? OR
    # governance_interaction_id = ?``).  Additive nullable column — no
    # primary-key rebuild (like v5/v13) — plus a one-time data backfill, so it
    # uses the callable path with the same ``sqlite_master`` + ``PRAGMA
    # table_info`` guards.  Lives in
    # :mod:`agents.memory._migration_governance_id`.  See
    # docs/issues/ISSUE-0102-closed-summary-episode-id-diverges-from-governance-interaction-id.md.
    (
        15,
        "ISSUE-0102: governance_interaction_id column on episodes + backfill",
        "",  # handled by _apply_migration_15()
    ),
    # Migration 16 (RFC 0037 PR 3) adds the §C protection/provenance columns
    # — ``protection_level`` (NOT NULL DEFAULT 'internal', which IS the §C
    # backfill), nullable ``source_channel_id``, and nullable
    # ``provenance_json`` (the multi-source shape, created now so the
    # RFC 0049 v0.4.0 pump needs no second migration) — to the three
    # channel-derived tiers (``episodes`` / ``facts`` / ``notes``), plus the
    # §E ``memory_projections`` table (written from RFC 0037 PR 6 on;
    # ``agent_id`` carried off the key as the RFC 0008 §H ACL / deletion
    # axis — see :mod:`agents.memory._migration_protection`).
    # Same callable-handler rationale as v7/v9/v10/v12 — ``ALTER TABLE ...
    # ADD COLUMN`` is not idempotent before SQLite 3.35.  Lives in
    # :mod:`agents.memory._migration_protection`.  See
    # docs/rfcs/0037-memory-confidentiality-channel-classification.md §C/§E.
    (
        16,
        "RFC 0037: protection_level/source_channel_id/provenance_json on "
        "episodes/facts/notes + memory_projections table",
        "",  # handled by _apply_migration_16()
    ),
    # Migration 17 ([ISSUE-0120]) folds a human's pre-ISSUE-0119 SPLIT
    # relationship rows back into one person: the group-channel publish path
    # used to deliver humans untyped, so their group traffic landed on an
    # agent-typed row beside the DM-written user-typed one.  A pure data
    # transform (not DDL), selected by the split's own fingerprint — an id
    # holding BOTH types — so a genuine agent peer is never rewritten.  Same
    # callable-handler + guard shape as the v14 identity backfill.  Lives in
    # :mod:`agents.memory._migration_split_participant`.  See
    # docs/issues/ISSUE-0120-backfill-split-participant-type-relationship-rows.md.
    (
        17,
        "ISSUE-0120: fold split agent-typed human relationship rows onto "
        "the user-typed row",
        "",  # handled by _apply_migration_17()
    ),
]
