"""
Relationship memory upkeep: the trust decay pass.

A standalone async function that takes an open ``aiosqlite.Connection``,
like the ones in :mod:`~agents.memory.relationship_mutations`, which it
was split from (that file was at the 500-line warning).  The split
mirrors the episodic tier's, where the retention passes
(:mod:`~agents.memory.episodic_retention`) sit apart from the per-turn
writes: this pass is upkeep, and nothing calls it on a turn.

Re-exported via :class:`~agents.memory.relationship.RelationshipMemory`.
"""

from __future__ import annotations

import logging
import math

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._write_boundary import execute_write

logger = logging.getLogger(__name__)

__all__ = ["apply_decay"]


async def apply_decay(
    db: aiosqlite.Connection,
    agent_id: str,
    decay_rate: float = 0.01,
    *,
    participant_type: str = "agent",
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> int:
    """Decay all trust scores toward 0.5 (neutral).

    Formula: ``new = old + decay_rate * (0.5 - old)``.
    Rows within 0.001 of neutral are skipped.  Decays all
    ``other_participant_type`` values uniformly (PR #120 F-5).

    ``principal_id`` (ISSUE-0081 PR 3; default :data:`DEFAULT_PRINCIPAL_ID`)
    scopes the maintenance pass to the active tenant — a decay run on one
    tenant's request must not move another tenant's trust.  ``epoch_id``
    (ISSUE-0085 PR 3; default :data:`DEFAULT_EPOCH_ID`) scopes it to the
    active run/test epoch for the same reason.

    Returns the number of relationships updated.
    """
    if math.isnan(decay_rate) or math.isinf(decay_rate) or not 0.0 < decay_rate <= 1.0:
        raise ValueError(f"decay_rate must be in (0.0, 1.0], got {decay_rate}")
    cursor = await execute_write(
        db,
        """
        UPDATE relationships
        SET trust_score = trust_score + ? * (0.5 - trust_score)
        WHERE participant_id = ? AND participant_type = ?
          AND principal_id = ?
          AND epoch_id = ?
          AND ABS(trust_score - 0.5) > 0.001
        """,
        (decay_rate, agent_id, participant_type, principal_id, epoch_id),
    )
    updated = cursor.rowcount
    if updated:
        logger.debug(
            "Applied trust decay (rate=%.3f) to %d relationships for %s",
            decay_rate,
            updated,
            agent_id,
        )
    return updated
