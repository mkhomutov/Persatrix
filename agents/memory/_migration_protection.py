"""RFC 0037 PR 3 migration v16 — protection level on the channel-derived tiers.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8 / v9 / v10 / v11 / v12
splits.

Adds the RFC 0037 §C provenance/protection columns to the **episodes**,
**facts**, and **notes** tiers in one version:

* ``protection_level TEXT NOT NULL DEFAULT 'internal'`` — the §A
  confidentiality level of the channel content the entry was derived
  from.  The ``'internal'`` default IS the §C backfill: pre-existing
  memory has no recorded channel classification reachable from this
  database (channel rows live in the orchestrator's channel store), and
  §B backfills every channel to ``internal`` too, so the common
  pre-existing case resolves consistently — neither silently ``public``
  (a disclosure) nor silently ``secret`` (which would withhold a
  persona's entire history from itself).  Notes are §C's documented
  honest exception: they carry no channel provenance at all, so every
  pre-migration note backfills ``internal`` regardless of where it was
  authored — an accepted, documented residual risk, superseded as notes
  are rewritten under the PR 4 gate.
* ``source_channel_id TEXT`` — nullable; the single channel the entry
  was derived from (NULL for pre-migration rows, synthesized notes, and
  non-channel scopes).
* ``provenance_json TEXT`` — nullable; the §C multi-source shape (list
  of contributing channel ids) created NOW so the RFC 0049 v0.4.0
  cross-scope pump needs no second migration.  Nothing writes it in
  v0.3.12.

Also creates the §E ``memory_projections`` table (used from RFC 0037
PR 6 on): zero or more lower-level abstracted restatements per protected
entry, keyed ``(entry_id, entry_tier, level)``.

The projections table carries ``agent_id`` like every other tier, even
though the natural key alone is unique (entry ids are uuid4).  Two
reasons, both structural rather than cosmetic:

* **RFC 0008 §H per-agent ACL.**  Personas share one SQLite file (and
  can share the connection — see ``_facts_erasure.py``'s per-agent
  DELETE contract), so every tier's reads and deletes are scoped by
  ``agent_id``.  A projections table without the column would force the
  PR 6 gate/writer to recover the owner by joining back through
  ``episodes`` / ``facts``, and would make the ACL a property of the
  caller's query rather than of the row.
* **Deletion.** SQLite foreign keys are OFF in this codebase (no
  ``PRAGMA foreign_keys`` anywhere), so ``ON DELETE CASCADE`` is not
  available: the eight parent-row delete paths (eviction, retention,
  ``episodic_crud``, notes pruning, fact supersession/erasure) cannot
  reach projections implicitly.  **PR 6 owns an explicit cleanup** —
  most sharply for RFC 0008 §H erasure, where a projection is an
  abstracted restatement of content the caller asked to erase.  That
  cleanup runs *after* the parent row is gone, so the owner has to be
  on the projection row itself; recovering it by join is impossible by
  then.  Adding the column here costs nothing (the table ships empty)
  and cannot be added later without a v17.

The stamping vocabulary's rule-(a) owner is
``agents/persona_runtime/classification.py`` (``normalize_for_stamp``);
this module deliberately does NOT import it — the memory package must
not depend on the persona subpackage (the import direction is
``persona_runtime → memory``).  :data:`PROTECTION_LEVEL_DEFAULT` below
is therefore a second spelling of the §A stamping default, pinned equal
to ``DEFAULT_CLASSIFICATION`` by
``tests/unit/python/test_protection_stamping.py`` (the cross-module
drift-pin discipline the Go↔Python lattice tables already use).

Idempotency / partial-restore safety: identical skeleton to v7 / v9 /
v10 / v12 — each table is guarded by a ``sqlite_master`` existence check
(short-circuits a partial-restore baseline missing a table) and a
``PRAGMA table_info`` column check before each ``ADD COLUMN`` (not
idempotent before SQLite 3.35); the ``CREATE TABLE IF NOT EXISTS`` makes
the projections half replay-safe on its own.
"""

from __future__ import annotations

import aiosqlite

#: The §A rule-(a) stamping default in the storage domain.  Kept in
#: lock-step with ``agents.persona_runtime.classification
#: .DEFAULT_CLASSIFICATION`` via the drift-pin test — see the module
#: docstring for why the value is spelled twice.
PROTECTION_LEVEL_DEFAULT = "internal"

#: The three channel-derived tiers gaining the §C columns.  Trusted
#: internal literals — never user input — so the f-string interpolation
#: below is safe (same contract as v7/v11/v12).
_PROTECTION_TABLES: tuple[str, ...] = ("episodes", "facts", "notes")


async def _apply_migration_16(db: aiosqlite.Connection) -> None:
    """RFC 0037 PR 3: §C protection/provenance columns + projections table.

    ``episodes`` / ``facts`` / ``notes`` each gain ``protection_level``
    (NOT NULL DEFAULT ``'internal'`` — the backfill), nullable
    ``source_channel_id``, and nullable ``provenance_json``; the §E
    ``memory_projections`` table is created empty.  Single tail
    ``db.commit()`` after the guarded DDL — same shape as v7–v12; the
    ``schema_version`` row is written by ``_apply_migrations`` after
    this returns, and the guards make a crash-replay safe.
    """
    for table in _PROTECTION_TABLES:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (table,),
        )
        if not await cursor.fetchone():
            continue
        cursor = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        if "protection_level" not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN protection_level "
                f"TEXT NOT NULL DEFAULT '{PROTECTION_LEVEL_DEFAULT}'",
            )
        if "source_channel_id" not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN source_channel_id TEXT",
            )
        if "provenance_json" not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN provenance_json TEXT",
            )

    # §E declassification projections (written from PR 6 on; the §D gate
    # reads them from the same PR).  The natural key IS the primary key:
    # one projection per (entry, tier, level) — a re-consolidation
    # replaces, never accumulates.  ``agent_id`` is deliberately NOT in
    # the key (entry ids are uuid4, so the natural key is already unique
    # cross-agent; keying on it would admit two owners for one entry) —
    # it is the RFC 0008 §H ACL / deletion axis, see the module docstring.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_projections (
            agent_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            entry_tier TEXT NOT NULL,
            level TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (entry_id, entry_tier, level)
        )
        """,
    )
    # The per-tier ``idx_<table>_agent`` analogue (v1 episodes / notes /
    # relationships): the ACL scope is the one axis every PR 6 read and
    # the erasure sweep filter on.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_projections_agent "
        "ON memory_projections(agent_id)",
    )

    await db.commit()
