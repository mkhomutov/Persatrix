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
prompt path (``memory_context``, ``cross_room: live`` — the default),
which passes ``reinforce=True``, and the shadow pass
(:mod:`agents.persona_runtime.episodes_shadow`, ``cross_room: shadow``),
which keeps the default ``reinforce=False`` so the shadow stays a pure
observer.  Every candidate still passes the RFC 0037 §D gate at the
caller.

Two deliberate differences from :meth:`EpisodicMemory.recall`:

* **Side-effect-free by default.**  No ``access_count`` bump and no
  ``last_accessed_at`` touch unless ``reinforce=True``.  Load-bearing
  for the shadow posture: the composite score includes
  ``access_count``, so a shadow read that reinforced would perturb the
  *live* ranking on later turns and shift the landed RFC 0044 goldens
  off their cassettes.  The PR 4 decision (deferred here by PR 3): the
  **promoted live read reinforces**, preserving the pre-promotion
  live-recall contract that access strengthens memory — same UPDATE
  shape as :meth:`EpisodicMemory.recall`, cross-room rows included
  (a recalled-and-used episode is a used episode wherever it was
  formed).
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
import time
from typing import TYPE_CHECKING

from ._epoch_filter import resolve_active_epoch
from ._principal_filter import resolve_active_principal
from ._session_filter import _resolve_session_list
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

    Returns rows in boosted-rank order.  ``reinforce=False`` (default —
    the shadow caller) never bumps ``access_count``; ``reinforce=True``
    (the live prompt path since the PR 4 promotion) applies the same
    access bump as :meth:`EpisodicMemory.recall` — see the module
    docstring for why the split is load-bearing.
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
        # Mirror ``EpisodicMemory.recall``'s bump exactly (UPDATE + the
        # in-memory object refresh) so the promoted live path keeps the
        # pre-promotion reinforcement contract byte-for-byte.
        now = time.time()
        ids = [e.id for e in episodes]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE episodes SET access_count = access_count + 1, "
            f"last_accessed_at = ? WHERE id IN ({placeholders})",
            [now, *ids],
        )
        await db.commit()
        for ep in episodes:
            ep.access_count += 1
            ep.last_accessed_at = now
    return episodes
