"""Room-first-RANKED episodic recall (RFC 0049 L1 amendment, Phase 1 PR 3).

The `L1 amendment
<../../docs/rfcs/0049-amendment-l1-cross-room-availability.md>`_ converts
the RFC 0031 §D session filter, as the episodic-recall default, from hard
exclusion into a **ranking cue**: same-room episodes (the resolved room +
the ``legacy`` carve-out — exactly what the §D wall admits today) are
boosted by :data:`~agents.memory._session_filter.ROOM_BOOST_FACTOR`,
other-room episodes become admissible candidates demoted by the missing
boost.  The dementia-test continuity bar is preserved as a *ranking*
property (same-room first at equal relevance) rather than by the wall.

This is the **gated cross-room episodic recall mode** the amendment
names.  Since the RFC 0049 PR 4 promotion it has two callers: the live
prompt path (``memory_context``, ``cross_room: live`` — the default)
and the shadow pass (:mod:`agents.persona_runtime.episodes_shadow`,
``cross_room: shadow``).  Both read with ``reinforce=False``.  Every
candidate still passes the RFC 0037 §D gate at the caller.

Two deliberate differences from :meth:`EpisodicMemory.recall`:

* **Side-effect-free by default.**  No ``access_count`` bump and no
  ``last_accessed_at`` touch unless ``reinforce=True``.  The composite
  score includes ``access_count``, so a read that reinforced would
  change the ranking of later turns.  A reinforcing shadow read would
  perturb the *live* ranking and shift the landed RFC 0044 goldens off
  their cassettes.  A reinforcing live read, as PR 4 shipped it, counted
  a use of every row it returned — before the §D gate, the audience
  check and the budget had chosen what the prompt would carry — so an
  episode the gate withheld gained a use on each turn it ranked in, and
  two unearned uses outweigh the same-room boost (ISSUE-0163).  Access
  still strengthens memory: once the prompt is assembled, the live
  path reinforces the episodes the budget admitted
  (:meth:`EpisodicMemory.reinforce`), the facts tier's rule — so an
  episode is reinforced when it is used, wherever it was formed.
  ``reinforce=True`` keeps the :meth:`EpisodicMemory.recall` contract,
  a use for every row returned, for a caller that uses all it reads.
* **Wall → boost.**  ``sessions``/``boost_sessions`` are mutually
  exclusive — enforced at the query helpers themselves since PR 4
  (``_reject_wall_and_boost``); this function always passes
  ``sessions=None`` + the resolved room list as ``boost_sessions``.
  ``epoch`` and ``principal`` remain strict-equality hard walls on
  every branch (cross-room is never cross-run or cross-tenant).

Free-function-taking-the-tier shape (the
:func:`~agents.memory.episodic_procedural.recall_procedures` precedent)
rather than a method: ``episodic.py`` sits at the 500-line cap, and the
class delegating here is exactly what the PR 4 flip adds if it wants the
method surface.  Private-attribute access is package-internal
(``agents.memory`` sibling, the ``_EpisodicNotesAPIMixin`` precedent).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ._epoch_filter import resolve_active_epoch
from ._principal_filter import resolve_active_principal
from ._session_filter import _resolve_session_list
from .episodic_crud import reinforce_episodes
from .episodic_queries import (
    MAX_RECALL_LIMIT,
    recall_fts5,
    recall_like,
    recall_recency,
    row_to_episode,
)
from .interactions import SUMMARY_PENDING_TEXT

if TYPE_CHECKING:
    from .episodic import EpisodicMemory
    from .episodic_queries import Episode

logger = logging.getLogger(__name__)

__all__ = ["recall_room_ranked"]


async def recall_room_ranked(
    memory: EpisodicMemory,
    query: str = "",
    *,
    limit: int = 10,
    min_importance: float = 0.0,
    min_score: float | None = None,
    reinforce: bool = False,
) -> list[Episode]:
    """Episodic recall with the §D room wall applied as ranking, not scope.

    Same query/limit/``min_score`` semantics as
    :meth:`EpisodicMemory.recall` (FTS5 → LIKE fallback → recency on an
    empty query; unfinalised ``[summary pending]`` rows dropped at the
    same chokepoint), with the session axis widened to every room of the
    active epoch+principal and the resolved room list applied as the
    same-room score boost.  The boost set resolves exactly like the live
    wall (``_resolve_session_list(None, …)`` — call-time ``session_scope``
    wins over the construction snapshot, ``legacy`` carve-out included),
    so wall and boost can never drift on what "same room" means.

    Returns rows in boosted-rank order.  ``reinforce=False`` (the default,
    and what both callers pass) never bumps ``access_count``;
    ``reinforce=True`` applies :meth:`EpisodicMemory.recall`'s access
    bump to every returned row — see the module docstring for why the
    prompt path reinforces after its gate instead (ISSUE-0163).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if limit > MAX_RECALL_LIMIT:
        logger.warning(
            "limit=%d exceeds maximum (%d), capping", limit, MAX_RECALL_LIMIT,
        )
        limit = MAX_RECALL_LIMIT
    if min_score is not None and not 0.0 <= min_score <= 1.0:
        raise ValueError(
            f"min_score must be in [0.0, 1.0] or None, got {min_score}",
        )
    boost = _resolve_session_list(None, memory._active_session_id)
    active_principal = resolve_active_principal(memory._active_principal_id)
    active_epoch = resolve_active_epoch(memory._active_epoch_id)
    db = memory._ensure_db()

    if query and memory.has_fts5:
        rows = await recall_fts5(
            db, memory.agent_id, query, limit, min_importance, min_score,
            sessions=None, boost_sessions=boost,
            principal_id=active_principal, epoch_id=active_epoch,
        )
    elif query:
        rows = await recall_like(
            db, memory.agent_id, query, limit, min_importance, min_score,
            sessions=None, boost_sessions=boost,
            principal_id=active_principal, epoch_id=active_epoch,
        )
    else:
        rows = await recall_recency(
            db, memory.agent_id, limit, min_importance,
            sessions=None, boost_sessions=boost,
            principal_id=active_principal, epoch_id=active_epoch,
        )

    episodes = [
        ep
        for ep in (row_to_episode(row) for row in rows)
        if ep.summary != SUMMARY_PENDING_TEXT
    ]
    if reinforce and episodes:
        # ``EpisodicMemory.recall``'s bump: the UPDATE, then the same
        # refresh of the returned objects.
        now = await reinforce_episodes(
            db, memory.agent_id, [e.id for e in episodes],
        )
        for ep in episodes:
            ep.access_count += 1
            ep.last_accessed_at = now
    return episodes
