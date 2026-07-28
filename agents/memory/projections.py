"""RFC 0037 §E declassification-projection storage (v0.3.12 PR 6).

Read/write access to the ``memory_projections`` table created dark by the
PR 3 migration (:mod:`agents.memory._migration_protection`): zero or more
lower-classified one-line restatements per protected memory entry, written
by the RFC 0020 close-consolidation call
(:mod:`agents.persona_runtime.finalize_close`) and served by the §D gate's
projection branch (:mod:`agents.persona_runtime.projection_branch`) in
place of a blunt withhold.

Free-function-taking-the-tier shape (the
:func:`~agents.memory.episodic_room_ranked.recall_room_ranked` precedent)
rather than methods: ``episodic.py`` sits at the 500-line cap, and the
:class:`~agents.memory.episodic.EpisodicMemory` connection is the one
handle every close-path and injection-path caller already holds.
Private-attribute access is package-internal (the sibling-module
precedent).

**The lattice stays out of this layer** (import direction is
``persona_runtime → memory``): writers pass levels they already resolved
via ``classification.levels_below_stamp`` and readers pass the
``classification.injectable_levels`` IN-set.  A stored level outside the
reader's set — including a corrupted label — falls out of the ``IN``
predicate, which is §A rule (c)'s withhold realised in SQL; picking the
*highest* admissible level is rank arithmetic and therefore belongs to
the persona-side caller.

Entry identity: ``entry_id`` for ``entry_tier='episode'`` is the
**agent-side interaction id** (``episodes.interaction_id``), not the
random ``episodes.id`` row uuid — the close path that writes projections
owns the interaction id before the episode row uuid exists on any object
it holds, and recall projects ``interaction_id`` onto every
:class:`~agents.memory.episode_types.Episode`, so both ends of the seam
key on the same stable value.  Future tiers (``fact`` / ``note``, the
RFC 0027 reflection producer) key on their own entry ids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .episodic import EpisodicMemory

__all__ = [
    "ENTRY_TIER_EPISODE",
    "projections_for",
    "replace_entry_projections",
]

#: ``entry_tier`` value for interaction-consolidation projections — the one
#: producer this release ships (RFC 0037 Phase 2).  ``fact`` / ``note``
#: arrive with the RFC 0027 reflection producer.
ENTRY_TIER_EPISODE: Final[str] = "episode"


async def replace_entry_projections(
    episodic: EpisodicMemory,
    *,
    entry_id: str,
    entry_tier: str,
    projections: Mapping[str, str],
    created_at: float,
) -> int:
    """Persist one entry's projection set, replacing any prior set.

    A re-consolidation replaces, never accumulates (the table's
    natural-key contract): every existing row THIS AGENT holds for
    ``(entry_id, entry_tier)`` is deleted first, so a later set that
    dropped a level cannot leave that level's stale text serving at the
    gate.  The delete is agent-scoped like the read — in a shared DB a
    neighbour's rows for a colliding entry id must never be clobbered
    by this agent's re-consolidation (a cross-agent key collision then
    surfaces as an ``IntegrityError``, caught by the best-effort close
    path — the safe direction).  Returns
    the number of rows written.  Levels arrive pre-resolved from the
    persona side (see module docstring); empty/whitespace texts are
    skipped here as a final guard even though the parser already drops
    them.
    """
    db = episodic._ensure_db()
    await db.execute(
        "DELETE FROM memory_projections "
        "WHERE agent_id = ? AND entry_id = ? AND entry_tier = ?",
        (episodic._agent_id, entry_id, entry_tier),
    )
    written = 0
    for level, text in projections.items():
        if not text.strip():
            continue
        await db.execute(
            """
            INSERT INTO memory_projections
                (agent_id, entry_id, entry_tier, level, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (episodic._agent_id, entry_id, entry_tier, level, text, created_at),
        )
        written += 1
    await db.commit()
    return written


async def projections_for(
    episodic: EpisodicMemory,
    *,
    entry_tier: str,
    entry_ids: Sequence[str],
    levels: Sequence[str],
) -> dict[str, list[tuple[str, str]]]:
    """Read the admissible projections for a batch of withheld entries.

    Returns ``{entry_id: [(level, text), ...]}`` restricted to rows whose
    ``level`` is in the caller-resolved ``levels`` IN-set (rule (c) in
    SQL — a corrupted stored level silently falls out) and scoped to this
    agent (the RFC 0008 §H ACL axis the ``agent_id`` column exists for).
    Entries with no admissible projection are absent from the result.
    The caller picks the highest-ranked level; this layer imposes no
    order beyond determinism.
    """
    if not entry_ids or not levels:
        return {}
    db = episodic._ensure_db()
    id_marks = ",".join("?" for _ in entry_ids)
    level_marks = ",".join("?" for _ in levels)
    out: dict[str, list[tuple[str, str]]] = {}
    async with db.execute(
        f"""
        SELECT entry_id, level, text
        FROM memory_projections
        WHERE agent_id = ?
          AND entry_tier = ?
          AND entry_id IN ({id_marks})
          AND level IN ({level_marks})
        ORDER BY entry_id, level
        """,
        (episodic._agent_id, entry_tier, *entry_ids, *levels),
    ) as cursor:
        async for row in cursor:
            out.setdefault(row[0], []).append((row[1], row[2]))
    return out
