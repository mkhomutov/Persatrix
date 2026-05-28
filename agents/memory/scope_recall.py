"""Shared recall + scope/tags filter helper (RFC 0011 PR 5 follow-up).

Single implementation of "recall episodes from
:class:`~agents.memory.episodic.EpisodicMemory`, then keep only those
matching an optional scope and/or AND-tag filter".  Both
:meth:`~agents.memory.facade.MemoryStore.retrieve_relevant` and the
persona-runtime channel-history tier in
:meth:`~agents.persona_runtime.memory_context._MemoryContextMixin._inject_memory_context`
delegate here, so the contract — over-fetch factor, scope precedence,
AND-tag semantics, hard cap to ``limit`` — is pinned in one place rather
than forked across two call sites.

Until SQL-side scope/tags filtering lands (tracked against the recall
chokepoint, not this helper), filtering is Python-side and over-fetches
``limit * TAG_SCOPE_OVERFETCH_FACTOR`` raw rows when a filter is active
so the AND contract still honours ``limit``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .episodic import EpisodicMemory
    from .episodic_queries import Episode

__all__ = [
    "TAG_SCOPE_OVERFETCH_FACTOR",
    "recall_with_scope_filter",
]


# Over-fetch factor applied to ``limit`` when a Python-side filter is
# active.  3× was tuned against PR-220 review M3: with FTS5 BM25
# ranking the cull can drop matches further down the result set, so a
# small ``limit`` would otherwise return zero even when matches exist.
# The constant is exported so call sites can assert against it in tests.
TAG_SCOPE_OVERFETCH_FACTOR: int = 3


async def recall_with_scope_filter(
    episodic: EpisodicMemory,
    query: str,
    *,
    limit: int,
    scope: str | None = None,
    tags: Iterable[str] | None = None,
    min_score: float | None = None,
    sessions: list[str] | str | None = None,
) -> list[Episode]:
    """Recall episodes and filter by ``scope`` / ``tags`` Python-side.

    Parameters
    ----------
    episodic:
        Underlying episodic memory.  The helper takes a reference
        rather than constructing one so callers control lifecycle.
    query:
        Free-text query forwarded to :meth:`EpisodicMemory.recall`.
    limit:
        Maximum number of episodes returned.  When a scope or tags
        filter is active the underlying ``recall`` is over-fetched by
        :data:`TAG_SCOPE_OVERFETCH_FACTOR` so the post-filter slice
        can still saturate ``limit``.
    scope:
        Optional scope string (e.g. ``"group:planning"``).  An episode
        matches when its column-level :attr:`Episode.scope` equals
        ``scope``; legacy rows with ``scope is None`` fall back to
        ``context["scope"]``.  The column wins when both are set.
    tags:
        Optional AND-tag filter.  An episode is admitted only when its
        tag set is a *superset* of the requested tags (RFC 0011 PR 5
        contract — do not change to OR without an RFC amendment).  An
        empty iterable matches everything.
    min_score:
        Forwarded to :meth:`EpisodicMemory.recall`.
    sessions:
        RFC 0031 §D recall filter (Phase 2 PR 2) — forwarded verbatim
        to :meth:`EpisodicMemory.recall`.  Orthogonal to ``scope`` /
        ``tags`` per `RFC 0031 §F
        <../../docs/rfcs/0031-per-session-namespacing-channels.md#f-interaction-with-rfc-0020-g-scope>`_:
        separate column, separate predicate, ANDed at the SQL layer
        — the session filter never widens the §G scope filter or
        vice versa.
    """
    required_tags = frozenset(tags or ())
    recall_limit = (
        limit * TAG_SCOPE_OVERFETCH_FACTOR
        if (required_tags or scope is not None)
        else limit
    )
    episodes = await episodic.recall(
        query,
        limit=recall_limit,
        min_score=min_score,
        sessions=sessions,
    )
    out: list[Episode] = []
    for ep in episodes:
        ep_tags = frozenset(ep.tags or ())
        if required_tags and not required_tags.issubset(ep_tags):
            continue
        # Column-level scope wins; ``context["scope"]`` is the legacy
        # fallback for pre-PR-220 rows whose writer did not pass the
        # column kwarg to ``store_episode``.
        entry_scope = ep.scope
        if entry_scope is None and isinstance(ep.context, dict):
            entry_scope = ep.context.get("scope")
        if scope is not None and entry_scope != scope:
            continue
        out.append(ep)
        if len(out) >= limit:
            break
    return out
