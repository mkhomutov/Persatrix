"""Data model + column constants for the episodic tier.

Split out of :mod:`agents.memory.episodic_queries` when the RFC 0037 PR 4
§C/§D projection columns pushed that module past the project's 500-line
review-friendly cap (see ``scripts/checks/file_size.py``), mirroring the
:mod:`agents.memory.fact_types` / :mod:`agents.memory.note_types`
precedent — the dataclass, the SELECT-column constants, and the row
conversion move together (they are one positional contract).
:mod:`agents.memory.episodic_queries` re-exports every name so
``from agents.memory.episodic_queries import Episode`` (and the
``EPISODE_SELECT`` / ``row_to_episode`` imports in ``episodic_crud`` /
``episodic_closed`` / ``episodic_retention``) keep working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ._migration_protection import PROTECTION_LEVEL_DEFAULT

__all__ = ["EPISODE_SELECT", "Episode", "row_to_episode"]


@dataclass
class Episode:
    """A single episodic memory entry."""

    id: str
    agent_id: str
    summary: str
    context: dict[str, Any]
    outcome: str | None
    importance: float
    access_count: int
    last_accessed_at: float | None
    tags: list[str]
    created_at: float
    compressed_at: float | None
    compression_level: int  # 0=raw, 1=summarized, 2=distilled
    # RFC 0020 §D column-level scope, projected onto the dataclass in
    # PR 2a so non-facade writers (``InteractionTracker``) are visible to
    # ``MemoryStore.retrieve_relevant(scope=...)`` filtering.  Defaults
    # to ``None`` so legacy rows without a scope value round-trip cleanly.
    scope: str | None = None
    # RFC 0021 PR 2 (this PR): expose the RFC 0020 §D interaction columns
    # to recall consumers so the persona-runtime memory packaging path can
    # render duration prefixes ("over 47 min, with Bob") on multi-turn
    # episodes.  All four default to ``None`` so legacy / single-turn rows
    # round-trip cleanly — recency rendering falls back to ``created_at``
    # when ``closed_at`` is missing.
    interaction_id: str | None = None
    started_at: float | None = None
    closed_at: float | None = None
    turn_count: int | None = None
    # ISSUE-0102 PR 2 (migration v15): the RFC 0030 governance interaction id
    # this episode was opened under — a different namespace from the agent-side
    # ``interaction_id`` above.  Nullable so legacy / DM / thread / non-channel
    # rows round-trip cleanly; the closed-interaction read filter matches it
    # alongside ``interaction_id`` so the channel-side id is look-up-able.
    governance_interaction_id: str | None = None
    # RFC 0037 §C/§D (migration v16, PR 4): the entry's protection level and
    # source channel, projected onto recall so the §D injection gate can rank
    # each candidate (and name its channel in the rule-(c) log).  The default
    # matches the v16 column DEFAULT so a hand-built fixture round-trips; a
    # row whose stored label is outside the §A vocabulary fails closed at
    # the gate (rule (c)), never here.
    protection_level: str = PROTECTION_LEVEL_DEFAULT
    source_channel_id: str | None = None


# Column list for SELECT queries — keeps row_to_episode() positional
# mapping stable when future migrations add columns to the episodes table.
_EPISODE_COLS = (
    "id", "agent_id", "summary", "context_json", "outcome",
    "importance", "access_count", "last_accessed_at",
    "tags_json", "created_at", "compressed_at", "compression_level",
    "scope",
    # RFC 0020 §D + RFC 0021 PR 2: interaction columns surfaced to recall.
    "interaction_id", "started_at", "closed_at", "turn_count",
    # ISSUE-0102 PR 2: governance interaction id surfaced to the read filter.
    "governance_interaction_id",
    # RFC 0037 §C (v16): protection columns surfaced to the §D gate.
    "protection_level", "source_channel_id",
)
EPISODE_SELECT = ", ".join(_EPISODE_COLS)
_EPISODE_SELECT_ALIASED = ", ".join(f"e.{c}" for c in _EPISODE_COLS)


def row_to_episode(row: aiosqlite.Row) -> Episode:
    """Convert a database row to an Episode dataclass."""
    return Episode(
        id=row[0],
        agent_id=row[1],
        summary=row[2],
        context=json.loads(row[3]) if row[3] else {},
        outcome=row[4],
        importance=row[5],
        access_count=row[6],
        last_accessed_at=row[7],
        tags=json.loads(row[8]) if row[8] else [],
        created_at=row[9],
        compressed_at=row[10],
        compression_level=row[11],
        scope=row[12],
        # RFC 0020 §D / RFC 0021 PR 2: trailing optional columns.  Legacy
        # rows that pre-date RFC 0020 PR 1 carry ``NULL`` here.
        interaction_id=row[13],
        started_at=row[14],
        closed_at=row[15],
        turn_count=row[16],
        # ISSUE-0102 PR 2 (migration v15): NULL on legacy / non-channel rows.
        governance_interaction_id=row[17],
        # RFC 0037 §C (migration v16): surfaced for the §D gate.
        protection_level=row[18],
        source_channel_id=row[19],
    )
