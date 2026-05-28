"""
Write/mutation helpers for RelationshipMemory.

Contains trust updates, decay, interaction recording, and trust seeding.
All functions accept an open ``aiosqlite.Connection`` and an ``agent_id``
string; they carry no object state and are safe to call from any async context.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
import uuid
from typing import Any

import aiosqlite
from opentelemetry import trace

from ..observability.metrics import try_get_instruments
from ..observability.spans import RELATIONSHIP_UPDATE_SPAN
from .relationship_queries import truncate_field, validate_other_id, validate_participant_types
from .relationship_types import (
    _DEFAULT_TRUST,
    _MAX_TRUST_DELTA,
)

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

__all__ = [
    "apply_decay",
    "record_interaction",
    "seed_trust",
    "update_trust",
]


async def update_trust(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    delta: float,
    reason: str,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
) -> float:
    """Update trust score. Returns new value (clamped to [0.0, 1.0]).

    *delta* is clamped to ±0.2 to prevent single interactions from
    swinging trust dramatically.

    .. note::

       The *reason* overwrites the previous ``notes`` value on the
       relationship row — only the most recent trust-change reason
       is retained.
    """
    validate_other_id(other_id)
    validate_participant_types(participant_type, other_participant_type)
    if math.isnan(delta) or math.isinf(delta):
        raise ValueError(f"delta must be a finite number, got {delta}")
    delta = max(-_MAX_TRUST_DELTA, min(_MAX_TRUST_DELTA, delta))
    reason = truncate_field(agent_id, reason, other_id, "reason")

    insert_trust = max(0.0, min(1.0, _DEFAULT_TRUST + delta))

    attrs: dict[str, Any] = {
        "agent.id": agent_id,
        "participant.id": other_id,
        "delta.kind": "trust",
        "delta.value": delta,
    }
    with _tracer.start_as_current_span(RELATIONSHIP_UPDATE_SPAN, attributes=attrs) as span:
        # SQL-level arithmetic in ON CONFLICT avoids TOCTOU race.
        # RETURNING avoids a separate get_trust() round-trip (SQLite >= 3.35).
        #
        # ISSUE-0061: drain the RETURNING row with ``execute_fetchall``
        # (one aiosqlite round-trip), not ``execute()`` + a separate
        # ``fetchone()``.  ``execute()`` steps a RETURNING statement only
        # to its first row, leaving the *write* VDBE active across the
        # ``await`` before ``fetchone()``; on RelationshipMemory's shared
        # connection a concurrent ``COMMIT`` in that gap raised "cannot
        # commit transaction - SQL statements in progress".  One
        # round-trip never suspends it — same fix as ISSUE-0055's
        # increment_interaction_count on the episodic connection.
        rows = list(await db.execute_fetchall(
            """
            INSERT INTO relationships
                (participant_id, participant_type,
                 other_participant_id, other_participant_type,
                 trust_score, interaction_count,
                 last_interaction_at, notes)
            VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
            ON CONFLICT(participant_id, participant_type,
                        other_participant_id, other_participant_type) DO UPDATE SET
                trust_score = MAX(0.0, MIN(1.0, relationships.trust_score + ?)),
                notes = ?
            RETURNING trust_score
            """,
            (
                agent_id,
                participant_type,
                other_id,
                other_participant_type,
                insert_trust,
                reason,
                delta,
                reason,
            ),
        ))
        await db.commit()
        row = rows[0] if rows else None

        # Set ``trust.new`` on every code path so the most diagnostic
        # attribute is always present.  When ``RETURNING`` yields no
        # row the INSERT branch fired, so the trust value is the
        # newly-inserted ``insert_trust`` (no ``MAX``/``MIN`` clamp
        # applies on insert because we already clamped above).
        if row is None:
            new_trust = insert_trust
        else:
            new_trust = float(row[0])
        span.set_attribute("trust.new", new_trust)
        if row is None:
            return insert_trust
        logger.debug(
            "Trust %s→%s: %.3f (delta=%.3f, reason=%s)",
            agent_id,
            other_id,
            new_trust,
            delta,
            reason,
        )
        return new_trust


async def apply_decay(
    db: aiosqlite.Connection,
    agent_id: str,
    decay_rate: float = 0.01,
    *,
    participant_type: str = "agent",
) -> int:
    """Decay all trust scores toward 0.5 (neutral).

    Formula: ``new = old + decay_rate * (0.5 - old)``.
    Rows within 0.001 of neutral are skipped.  Decays all
    ``other_participant_type`` values uniformly (PR #120 F-5).

    Returns the number of relationships updated.
    """
    if math.isnan(decay_rate) or math.isinf(decay_rate) or not 0.0 < decay_rate <= 1.0:
        raise ValueError(f"decay_rate must be in (0.0, 1.0], got {decay_rate}")
    cursor = await db.execute(
        """
        UPDATE relationships
        SET trust_score = trust_score + ? * (0.5 - trust_score)
        WHERE participant_id = ? AND participant_type = ?
          AND ABS(trust_score - 0.5) > 0.001
        """,
        (decay_rate, agent_id, participant_type),
    )
    updated = cursor.rowcount
    if updated:
        await db.commit()
        logger.debug(
            "Applied trust decay (rate=%.3f) to %d relationships for %s",
            decay_rate,
            updated,
            agent_id,
        )
    return updated


async def record_interaction(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    interaction_type: str,
    outcome: str | None = None,
    sentiment: float = 0.0,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
    session_id: str = "legacy",
) -> str:
    """Record an interaction with another participant.

    Inserts into the ``interactions`` table and increments the
    ``interaction_count`` on the relationship. Creates the
    relationship row if it doesn't exist.

    RFC 0031 Phase 1: ``session_id`` (default ``"legacy"``) tags the
    relationship row on first creation.  Subsequent calls keep the
    first-seen value — the relationships row is a stable per-pair
    identity, so we treat ``session_id`` like ``trust_score`` (write on
    INSERT, preserved by the conflict path) rather than like
    ``last_interaction_at`` (refreshed on every interaction).  Phase 1
    ships no recall-side filtering; the column exists so Phase 2 has a
    column + index to filter on.

    Returns the generated interaction ID.
    """
    validate_other_id(other_id)
    validate_participant_types(participant_type, other_participant_type)
    if not interaction_type or not interaction_type.strip():
        raise ValueError("interaction_type must not be empty")
    if math.isnan(sentiment) or math.isinf(sentiment):
        raise ValueError(f"sentiment must be a finite number, got {sentiment}")
    sentiment = max(-1.0, min(1.0, sentiment))

    if outcome:
        outcome = truncate_field(agent_id, outcome, other_id, "outcome")

    interaction_id = str(uuid.uuid4())
    now = time.time()

    await db.execute(
        """
        INSERT INTO interactions
            (id, participant_id, participant_type,
             other_participant_id, other_participant_type,
             interaction_type, outcome, sentiment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interaction_id,
            agent_id,
            participant_type,
            other_id,
            other_participant_type,
            interaction_type,
            outcome,
            sentiment,
            now,
        ),
    )

    # Upsert relationship row: create if missing, increment count.
    # RFC 0031 Phase 1: ``session_id`` is written on INSERT only; the
    # ON CONFLICT branch deliberately omits it so the row tracks the
    # first-seen session and a future cross-session interaction does
    # not silently overwrite the per-row tag (recall semantics for that
    # use case live on the interactions table in Phase 2).
    await db.execute(
        """
        INSERT INTO relationships
            (participant_id, participant_type,
             other_participant_id, other_participant_type,
             trust_score, interaction_count,
             last_interaction_at, notes, session_id)
        VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?)
        ON CONFLICT(participant_id, participant_type,
                    other_participant_id, other_participant_type) DO UPDATE SET
            interaction_count = interaction_count + 1,
            last_interaction_at = ?
        """,
        (
            agent_id,
            participant_type,
            other_id,
            other_participant_type,
            _DEFAULT_TRUST,
            now,
            session_id,
            now,
        ),
    )
    await db.commit()
    # RFC 0031 Phase 1 — increment the per-session write counter (Python
    # mirror of the orchestrator-side ``sessions.writes``).  Recorded on
    # every ``record_interaction`` call, not just the first-seen INSERT
    # branch, so dashboards see one tick per persona-level interaction
    # event regardless of whether the conflict path fired.
    #
    # RFC 0031 Phase 2 PR 3 (F17 carry-forward): wrapped in
    # ``contextlib.suppress`` so an OTEL backend exception cannot
    # surface as a write failure after ``db.commit()`` has already
    # persisted the row — same failure-isolation contract as
    # :meth:`EpisodicMemory.store_episode` (PR #337 M1) and
    # :meth:`NoteStore.store_note` (PR 449).
    with contextlib.suppress(Exception):
        inst = try_get_instruments()
        if inst is not None:
            inst.sessions_writes.add(
                1,
                attributes={
                    "session_id": session_id,
                    "agent.id": agent_id,
                    "surface": "relationship",
                },
            )
    return interaction_id


async def seed_trust(
    db: aiosqlite.Connection,
    agent_id: str,
    config_relationships: list[dict[str, Any]],
    *,
    session_id: str = "legacy",
) -> None:
    """Seed trust scores from agent config. Never overwrites existing rows.

    ``session_id`` (RFC 0031 Phase 1; default ``"legacy"``) tags every
    newly-inserted seed row.  The caller (persona-runtime
    ``initialize_memory``) passes its resolved ``PERSATRIX_SESSION_ID``
    so MT-SESSION-001 Step 7 sees ``run-a`` on a config-seeded row
    rather than the column-default ``legacy``.  Existing rows are still
    untouched — INSERT OR IGNORE preserves the first-seen contract.
    """
    for entry in config_relationships:
        other_id = entry.get("agent_id")
        trust_level = entry.get("trust_level")
        if other_id is None or trust_level is None:
            logger.debug(
                "Skipping config relationship entry: "
                "missing agent_id or trust_level: %r",
                entry,
            )
            continue
        try:
            trust_level = max(0.0, min(1.0, float(trust_level)))
        except (TypeError, ValueError):
            logger.warning(
                "Invalid trust_level for %s: %r, skipping",
                other_id,
                trust_level,
            )
            continue

        # INSERT OR IGNORE — only seeds if no row exists.
        # Types hardcoded to 'agent': config schema only has agent_id
        # entries (PR #120 F-6).
        await db.execute(
            """
            INSERT OR IGNORE INTO relationships
                (participant_id, participant_type,
                 other_participant_id, other_participant_type,
                 trust_score, interaction_count,
                 last_interaction_at, notes, session_id)
            VALUES (?, 'agent', ?, 'agent', ?, 0, NULL, NULL, ?)
            """,
            (agent_id, other_id, trust_level, session_id),
        )
    await db.commit()
