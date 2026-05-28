"""Free-function helpers + mixin backing
:meth:`MemoryStore.publish_to_pool` and :meth:`MemoryStore.read_from_pool`.

Kept in a separate module so ``agents/memory/facade.py`` and
``agents/memory/shared_pool.py`` both stay under the repo line cap.
RFC 0008 PR plan PR 4.
"""

from __future__ import annotations

from collections.abc import Iterable

from .shared_pool import (
    _MIN_CONFIDENCE_OVERFETCH_FACTOR,
    SharedMemoryPermissionError,
    SharedPoolEntry,
    SharedPoolRegistry,
    _record_denied,
)

# PR #223 deep-review (pass 2) S3-tag: ``read_via_facade`` applies the
# AND-tag filter *after* ``pool.read`` has trimmed to ``limit``.  Without
# an over-fetch, a caller asking for ``limit=N`` with a tag set could
# receive fewer than N entries even when N matches exist deeper in the
# ranking — the same trim-after-limit class as PR-220 review M3 (tags)
# and PR-223 pass-1 S3 (min_confidence).  Reuse the pool-side factor so
# both trust filters share one knob; multiplied with the pool's own
# over-fetch when ``min_confidence`` is also set, which is acceptable
# (the FTS5 recall bound is the only ceiling and the result is trimmed
# back to ``limit`` below).
_TAG_FILTER_OVERFETCH_FACTOR = _MIN_CONFIDENCE_OVERFETCH_FACTOR


async def publish_via_facade(
    registry: SharedPoolRegistry | None,
    agent_id: str,
    pool_name: str,
    content: str,
    *,
    confidence: float,
    tags: Iterable[str] = (),
    session_id: str = "legacy",
) -> str:
    """Curated isolated→shared publish path (RFC 0008 §H).

    Rejects publication into ``sensitive: true`` pools regardless of
    writer ACL — RFC §H safety constraint #3.  ``source_agent`` is
    framework-injected from *agent_id*; callers cannot spoof it.

    ``session_id`` (RFC 0031 Phase 1; default ``"legacy"``) tags the
    underlying episode.  The facade-mixin caller threads its own
    construction-time default so a sub-agent publishing into a shared
    pool inherits the same operator-namespace as its parent.
    """
    if registry is None:
        raise SharedMemoryPermissionError(
            f"shared pool {pool_name!r} is not configured",
            reason="unknown_pool",
        )
    pool = registry.get(pool_name)
    if pool.config.sensitive:
        _record_denied(pool_name, agent_id, operation="publish")
        raise SharedMemoryPermissionError(
            f"pool {pool_name!r} is sensitive — isolated→shared publish "
            "is forbidden by RFC 0008 §H safety constraint #3",
            reason="sensitive_pool_isolation",
        )
    # PR #223 deep-review S1: ``SharedMemoryPool.write`` binds
    # ``source_agent`` 1-for-1 to ``agent_id`` (no override knob), which
    # is exactly the framework-injection guarantee the facade promises.
    return await pool.write(
        agent_id, content,
        confidence=confidence, tags=tags, session_id=session_id,
    )


async def read_via_facade(
    registry: SharedPoolRegistry | None,
    agent_id: str,
    pool_name: str,
    query: str,
    *,
    limit: int = 10,
    min_confidence: float | None = None,
    tags: Iterable[str] | None = None,
    sessions: list[str] | str | None = "*",
) -> list[SharedPoolEntry]:
    """Read entries with consumer-side trust + AND-tag filter.

    ``sessions`` (RFC 0031 Phase 2 PR 4) defaults to ``"*"`` —
    cross-session, per `ISSUE-0078
    <../../docs/issues/ISSUE-0078-shared-pool-read-session-filter-policy.md>`_
    Policy A — and is forwarded verbatim to
    :meth:`SharedMemoryPool.read`.  PR #451 deep-review M2 moved the
    policy from the mixin method to the data layer; this helper now
    matches the tier default so a direct caller cannot accidentally
    trigger session narrowing by omitting ``sessions=``.
    """
    if registry is None:
        raise SharedMemoryPermissionError(
            f"shared pool {pool_name!r} is not configured",
            reason="unknown_pool",
        )
    pool = registry.get(pool_name)
    # Over-fetch when an AND-tag filter is active so the post-filter
    # result honours ``limit`` (PR #223 pass-2 review S3-tag).  The
    # pool's own ``min_confidence`` over-fetch composes with this one
    # (the recall ceiling is bounded by the FTS5 LIMIT only); we trim
    # back to ``limit`` after the AND-tag filter below.
    recall_limit = limit * _TAG_FILTER_OVERFETCH_FACTOR if tags else limit
    entries = await pool.read(
        agent_id, query, limit=recall_limit,
        min_confidence=min_confidence, sessions=sessions,
    )
    if tags:
        required = frozenset(tags)
        entries = [e for e in entries if required.issubset(e.tags)][:limit]
    return entries


class SharedPoolFacadeMixin:
    """Mixin that adds ``publish_to_pool`` / ``read_from_pool`` methods.

    Expects the host class to provide ``_shared_pools``, ``_agent_id``,
    ``_session_id`` (RFC 0031 Phase 1 facade-level default), and
    ``_require_initialised()``.  Lives here (not on
    :class:`MemoryStore` directly) to keep ``facade.py`` under the
    repo line cap.
    """

    _shared_pools: SharedPoolRegistry | None
    _agent_id: str
    # RFC 0031 Phase 1: facade-level default for the operator-namespace
    # tag (see :class:`agents.memory.facade.MemoryStore`).
    _session_id: str

    def _require_initialised(self) -> None: ...  # provided by host

    async def publish_to_pool(
        self, pool_name: str, content: str, *,
        confidence: float, tags: Iterable[str] = (),
        session_id: str | None = None,
    ) -> str:
        self._require_initialised()
        return await publish_via_facade(
            self._shared_pools, self._agent_id, pool_name, content,
            confidence=confidence, tags=tags,
            session_id=(
                session_id if session_id is not None else self._session_id
            ),
        )

    async def read_from_pool(
        self, pool_name: str, query: str, *,
        limit: int = 10,
        min_confidence: float | None = None,
        tags: Iterable[str] | None = None,
        sessions: list[str] | str | None = "*",
    ) -> list[SharedPoolEntry]:
        """Consumer-side shared-pool read.

        ``sessions`` (RFC 0031 Phase 2 PR 4 — `ISSUE-0078
        <../../docs/issues/ISSUE-0078-shared-pool-read-session-filter-policy.md>`_
        Policy A — cross-session default for shared pools): defaults to
        ``"*"`` so a row written under any session is visible to any
        reader (RFC 0008 §H — shared pools are cross-agent /
        cross-session by design).  The default lives at
        :meth:`SharedMemoryPool.read` itself; this method is shape-
        preserving and passes ``sessions`` verbatim.  An explicit list
        opts in to session-scoped reading; ``"*"`` is the documented
        no-filter sentinel; ``[]`` raises :class:`ValueError` from the
        tier helper.  (PR #451 deep-review M2 — moved the policy to one
        place; the facade is now a pure pass-through.)
        """
        self._require_initialised()
        return await read_via_facade(
            self._shared_pools, self._agent_id, pool_name, query,
            limit=limit, min_confidence=min_confidence, tags=tags,
            sessions=sessions,
        )


__all__ = [
    "SharedPoolFacadeMixin",
    "publish_via_facade",
    "read_via_facade",
]
