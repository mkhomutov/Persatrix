"""Free-function helpers + mixin backing
:meth:`MemoryFacade.publish_to_pool` and :meth:`MemoryFacade.read_from_pool`.

Kept in a separate module so ``agents/memory/facade.py`` and
``agents/memory/shared_pool.py`` both stay under the repo line cap.
RFC 0008 PR plan PR 4.
"""

from __future__ import annotations

from collections.abc import Iterable

from .shared_pool import (
    SharedMemoryPermissionError,
    SharedPoolEntry,
    SharedPoolRegistry,
    _record_denied,
)


async def publish_via_facade(
    registry: SharedPoolRegistry | None,
    agent_id: str,
    pool_name: str,
    content: str,
    *,
    confidence: float,
    tags: Iterable[str] = (),
) -> str:
    """Curated isolated→shared publish path (RFC 0008 §H).

    Rejects publication into ``sensitive: true`` pools regardless of
    writer ACL — RFC §H safety constraint #3.  ``source_agent`` is
    framework-injected from *agent_id*; callers cannot spoof it.
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
        confidence=confidence, tags=tags,
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
) -> list[SharedPoolEntry]:
    """Read entries with consumer-side trust + AND-tag filter."""
    if registry is None:
        raise SharedMemoryPermissionError(
            f"shared pool {pool_name!r} is not configured",
            reason="unknown_pool",
        )
    pool = registry.get(pool_name)
    entries = await pool.read(
        agent_id, query, limit=limit, min_confidence=min_confidence,
    )
    if tags:
        required = frozenset(tags)
        entries = [e for e in entries if required.issubset(e.tags)]
    return entries


class SharedPoolFacadeMixin:
    """Mixin that adds ``publish_to_pool`` / ``read_from_pool`` methods.

    Expects the host class to provide ``_shared_pools``, ``_agent_id``,
    and ``_require_initialised()``.  Lives here (not on
    :class:`MemoryFacade` directly) to keep ``facade.py`` under the
    repo line cap.
    """

    _shared_pools: SharedPoolRegistry | None
    _agent_id: str

    def _require_initialised(self) -> None: ...  # provided by host

    async def publish_to_pool(
        self, pool_name: str, content: str, *,
        confidence: float, tags: Iterable[str] = (),
    ) -> str:
        self._require_initialised()
        return await publish_via_facade(
            self._shared_pools, self._agent_id, pool_name, content,
            confidence=confidence, tags=tags,
        )

    async def read_from_pool(
        self, pool_name: str, query: str, *,
        limit: int = 10,
        min_confidence: float | None = None,
        tags: Iterable[str] | None = None,
    ) -> list[SharedPoolEntry]:
        self._require_initialised()
        return await read_via_facade(
            self._shared_pools, self._agent_id, pool_name, query,
            limit=limit, min_confidence=min_confidence, tags=tags,
        )


__all__ = [
    "SharedPoolFacadeMixin",
    "publish_via_facade",
    "read_via_facade",
]
