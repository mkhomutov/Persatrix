"""
Episode write helpers for EpisodicMemory: the INSERT of a new episode and
the close-path summary UPDATE.

Split from :mod:`agents.memory.episodic_queries`, which keeps the read side
(recall queries, the Episode model, agent-state helpers), when the
ISSUE-0159 recall changes took that module past the 500-line cap. Like it,
every function takes an open ``aiosqlite.Connection`` and an ``agent_id``
and carries no object state.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from ..clock import agent_now
from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from .interaction_janitor import SUMMARY_PENDING_TEXT

__all__ = ["insert_episode", "update_episode_summary"]


async def insert_episode(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    summary: str,
    context: dict[str, Any],
    outcome: str | None,
    importance: float,
    tags: list[str] | None,
    interaction_id: str | None,
    started_at: float | None,
    closed_at: float | None,
    turn_count: int | None,
    session_id: str,
    scope: str | None,
    governance_interaction_id: str | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
    protection_level: str = PROTECTION_LEVEL_DEFAULT,
    source_channel_id: str | None = None,
    speaker_id: str | None = None,
) -> str:
    """INSERT one episode row and COMMIT; return the generated episode id.

    Extracted from :meth:`EpisodicMemory.store_episode` so the episode
    INSERT sits beside the sibling write helper
    :func:`update_episode_summary`, keeping ``episodic.py`` within the
    500-line file-size cap.

    ``protection_level`` / ``source_channel_id`` (RFC 0037 §C — v16)
    persist verbatim; see :meth:`EpisodicMemory.store_episode` for the
    stamp-site normalization contract.

    ``speaker_id`` (ISSUE-0131 — v18) is the PROJECTION of the
    ``(principal, speaker, scope)`` record key's speaker half: WHO said
    the content this row was derived from.  ``None`` for a row with no
    speaker (a tick, a single-turn scope, an operator/test write) and
    for every pre-v18 row, whose speaker is genuinely unknowable — the
    aggregate it came from spanned the whole room, which is the defect
    the key exists to fix.  Sound only because a record is single-speaker
    by construction; the one §G breach is excluded upstream
    (``close_entries`` states the argument).

    The INSERT is plain DML — stepped to completion inside ``execute()``
    with no VDBE left active — so a concurrent ``COMMIT`` on the shared
    connection cannot race it (ISSUE-0055).
    """
    # PR #849 review round 3: the ``"" == no speaker → NULL`` convention
    # is enforced HERE, at the storage boundary, so a direct caller that
    # passes the tracker's empty-string sentinel cannot mint a third
    # speaker state (same rationale as subject canonicalisation in
    # ``_facts_write``: call-site discipline does not cover direct
    # writers).  The projection call sites still pass ``or None`` — that
    # keeps their intent readable; this line makes it structural.
    speaker_id = speaker_id or None
    episode_id = str(uuid.uuid4())
    now = agent_now()
    await db.execute(
        """
        INSERT INTO episodes
            (id, agent_id, summary, context_json, outcome,
             importance, access_count, last_accessed_at,
             tags_json, created_at, compressed_at, compression_level,
             interaction_id, started_at, closed_at, turn_count, scope,
             session_id, principal_id, epoch_id, governance_interaction_id,
             protection_level, source_channel_id, speaker_id)
        VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?)
        """,
        (
            episode_id,
            agent_id,
            summary,
            json.dumps(context),
            outcome,
            importance,
            json.dumps(tags or []),
            now,
            interaction_id,
            started_at,
            closed_at,
            turn_count,
            scope,
            session_id,
            principal_id,
            epoch_id,
            governance_interaction_id,
            protection_level,
            source_channel_id,
            speaker_id,
        ),
    )
    await db.commit()
    return episode_id


async def update_episode_summary(
    db: aiosqlite.Connection, agent_id: str,
    interaction_id: str, summary: str,
) -> bool:
    """Replace ``[summary pending]`` for an episode (RFC 0020 PR 4 close-path).

    Agent-scoped UPDATE that *only* matches rows still carrying the
    :data:`SUMMARY_PENDING_TEXT` sentinel — the janitor's
    :data:`SUMMARY_UNAVAILABLE_TEXT` verdict must be final once written
    so a late-successful Phase-2 LLM completion cannot overwrite it
    (PR 6 review #20).  Returns ``True`` iff this UPDATE replaced a
    pending row; callers use a ``False`` return to skip the
    relationship-bump and auto-reflect tick so the failure counter
    cannot double-increment for the same interaction.

    The single-writer invariant guarantees ``summary`` is non-empty at
    every production call site (the summariser returns either LLM text
    or :data:`SUMMARY_UNAVAILABLE_TEXT`); revalidating here was dead
    code (PR 6 review #22).
    """
    cursor = await db.execute(
        "UPDATE episodes SET summary = ? "
        "WHERE agent_id = ? AND interaction_id = ? AND summary = ?",
        (summary, agent_id, interaction_id, SUMMARY_PENDING_TEXT),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0
