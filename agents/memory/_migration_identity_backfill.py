"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) migration v14 — one-time
**backfill** of pre-cutover ``contact:*`` notes onto relationship identity.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8–v13 splits.

**What & why.**  PR D1 (migration v13) added the ``identity`` column; D2/D3
moved person identity (name / role / stable preferences) onto the
cross-room ``relationships`` tier and retired the F-7 Option-A
``contact:*`` *recall* carve-out.  But identity *captured before the
cutover* still lives only in room-scoped ``contact:<id>`` notes — which the
relationship-tier render no longer reads.  This migration reads those
legacy notes and merges them onto the matching relationship row so a
persona does not lose what it learned about a person pre-cutover (the
[ISSUE-0093](../../docs/issues/ISSUE-0093-person-identity-cross-room-tier.md)
"don't lose identity" goal).

**The participant-type design choice (why this is its own PR, split from
D3).**  A ``contact:<id>`` note records ``agent_id`` (the persona =
``participant_id``, always ``participant_type='agent'``), ``principal_id``,
``epoch_id`` and — from the topic suffix — ``other_participant_id``.  The
*only* relationship-PK axis it does **not** record is
``other_participant_type``.  Rather than guess it (and never fabricating the
tenant/epoch axes the design's isolation rests on), the backfill resolves
it two ways:

* **Inherit** — merge identity onto *every* existing relationship row for
  the note's recorded ``(agent_id, principal_id, epoch_id, other_id)``
  tuple, whatever ``other_participant_type`` those rows carry.  Identity
  lands exactly on the row(s) recall later anchors on; no type is invented.
* **Orphan fallback** — when no relationship row exists for the tuple,
  create one under :func:`agents.sender_type.normalize_sender_type` of
  ``None`` (``"agent"`` — the same default both the write-through and the
  recall side fall back to when the sender type is unbound), at neutral
  trust, interaction-free.  This preserves the identity without fabricating
  a tenant/epoch axis.

**Pure helpers, raw SQL.**  Parsing (:func:`parse_identity_fields`) and the
merge rule (:func:`merge_identity`) are pure and DB-free, so this data
transform stays self-contained — it reads/writes the two tables with
individual ``db.execute`` calls and a single tail ``commit`` (the
``executescript``-implicit-COMMIT caveat in :mod:`agents.memory.migrations`
does not apply to a callable handler).

Idempotency / partial-restore safety: a ``sqlite_master`` existence check
on both tables short-circuits a partial-restore baseline missing either,
and a ``PRAGMA table_info`` check confirms the ``identity`` column (v13)
landed before this runs.  The transform itself is idempotent: re-running
merges identical parsed fields (scalar last-writer-wins is stable;
``prefs`` / ``raw`` unions de-duplicate), and a re-run finds the
orphan-created row as an *existing* row and merges onto it (no duplicate
insert) — so a crash-replay between this handler and the ``schema_version``
record is a clean no-op.  No index is created: identity is read alongside
the relationship row recall already anchors on the participant tuple.
"""

from __future__ import annotations

import json

import aiosqlite

from ..sender_type import normalize_sender_type
from .identity_parse import parse_identity_fields
from .relationship_types import _DEFAULT_TRUST, merge_identity

__all__ = ["_apply_migration_14"]

#: ``store_note`` topic prefix whose subject is a *person* — the same
#: convention :mod:`agents.tools.identity_write_through` routes live.  Kept
#: local: this migration reads the legacy on-disk shape, not the live path.
_CONTACT_TOPIC_PREFIX = "contact:"


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _has_column(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return column in {row[1] for row in await cursor.fetchall()}


async def _apply_migration_14(db: aiosqlite.Connection) -> None:
    """RFC 0031 amendment (F-7 Option D): backfill ``contact:*`` notes →
    relationship identity.  See the module docstring for the full contract.

    No-op (clean return) when either table is absent (partial-restore
    baseline) or the ``identity`` column has not yet landed — never crashing
    the migration chain.  Single tail ``db.commit()``; ``_apply_migrations``
    records ``schema_version`` after this returns.
    """
    if not await _table_exists(db, "notes") or not await _table_exists(
        db, "relationships",
    ):
        return
    if not await _has_column(db, "relationships", "identity"):
        return

    # Read every legacy contact note in chronological order so that, when
    # several notes describe one person, the *last* note's scalar fields win
    # (matching the live write-through's per-turn merge order).
    cursor = await db.execute(
        """
        SELECT agent_id, principal_id, epoch_id, topic, content
        FROM notes
        WHERE topic LIKE 'contact:%'
        ORDER BY created_at ASC, id ASC
        """,
    )
    note_rows = await cursor.fetchall()

    # Accumulate parsed identity per recorded tuple — the four axes the note
    # *does* pin.  ``other_participant_type`` is resolved per-tuple below.
    by_tuple: dict[tuple[str, str, str, str], dict] = {}
    for agent_id, principal_id, epoch_id, topic, content in note_rows:
        other_id = topic[len(_CONTACT_TOPIC_PREFIX):].strip()
        if not other_id:
            continue
        fields = parse_identity_fields(content or "")
        if not fields:
            continue
        key = (agent_id, principal_id, epoch_id, other_id)
        by_tuple[key] = merge_identity(by_tuple.get(key, {}), fields)

    for (agent_id, principal_id, epoch_id, other_id), fields in by_tuple.items():
        if not fields:
            continue
        await _backfill_tuple(
            db, agent_id, principal_id, epoch_id, other_id, fields,
        )

    await db.commit()


async def _backfill_tuple(
    db: aiosqlite.Connection,
    agent_id: str,
    principal_id: str,
    epoch_id: str,
    other_id: str,
    fields: dict,
) -> None:
    """Merge ``fields`` onto the relationship row(s) for one recorded tuple.

    Inherits ``other_participant_type`` from each existing row; falls back to
    a single neutral orphan row under the default type when none exist.
    """
    cursor = await db.execute(
        """
        SELECT other_participant_type, identity
        FROM relationships
        WHERE participant_id=? AND participant_type='agent'
          AND other_participant_id=? AND principal_id=? AND epoch_id=?
        """,
        (agent_id, other_id, principal_id, epoch_id),
    )
    existing = await cursor.fetchall()

    if existing:
        for other_type, identity_json in existing:
            current = json.loads(identity_json) if identity_json else {}
            merged = merge_identity(current, fields)
            if merged == current:
                continue  # idempotent: nothing new to write
            await db.execute(
                """
                UPDATE relationships SET identity=?
                WHERE participant_id=? AND participant_type='agent'
                  AND other_participant_id=? AND other_participant_type=?
                  AND principal_id=? AND epoch_id=?
                """,
                (
                    json.dumps(merged), agent_id, other_id, other_type,
                    principal_id, epoch_id,
                ),
            )
        return

    # Orphan: no relationship row for this person yet.  Create a neutral,
    # interaction-free row under the default sender type so the identity is
    # preserved (the immediacy render handles interaction-free rows).  The
    # principal / epoch come from the note — never fabricated.
    await db.execute(
        """
        INSERT INTO relationships
            (participant_id, participant_type,
             other_participant_id, other_participant_type,
             trust_score, interaction_count, last_interaction_at,
             notes, identity, principal_id, epoch_id)
        VALUES (?, 'agent', ?, ?, ?, 0, NULL, NULL, ?, ?, ?)
        """,
        (
            agent_id, other_id, normalize_sender_type(None),
            _DEFAULT_TRUST, json.dumps(fields), principal_id, epoch_id,
        ),
    )
