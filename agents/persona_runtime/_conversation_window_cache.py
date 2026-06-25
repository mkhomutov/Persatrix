"""RFC 0034 §F — the conversation-window cached fetch layer.

Carved out of :mod:`conversation_window` (which is at the 500-line review cap)
so the cache concern lives in one cohesive module: the bounded LRU
(:class:`_WindowCache`, RFC 0034 Phase 3), its process-global singleton, and
:func:`_fetch_window` — the cache's only consumer, which serves a cached window
or issues a fresh history fetch and degrades gracefully on failure.

The in-process cache maps ``(channel_id, limit, agent_id)`` to the last
``(message_id, raw_rows)`` seen for that channel at that fetch limit *for that
persona*. A call whose ``event.message_id`` matches the cached id — same
channel, limit, AND agent — skips the network fetch and re-uses the rows; a
newer ``message_id`` overwrites the entry (the "cheapest possible" invalidation
RFC §F specifies).

``limit`` AND ``agent_id`` are both in the key for multi-persona correctness on
a shared (group) channel:

* ``limit`` (RFC 0034 Phase 2): a small-``max_turns`` persona must not prime an
  undersized row set that a large-``max_turns`` peer is served.
* ``agent_id`` (RFC 0036 §G): the fetch passes ``as_participant=agent_id``, so
  the rows are membership-scoped per persona — a re-added persona sees a
  gap-trimmed window a continuously-present peer does not, and must never be
  served the peer's rows. The rows are therefore agent-SPECIFIC, not the
  agent-independent cache Phase 1 shipped.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from ..observability._metrics_conversation_window import (
    record_cache_access,
    record_cache_eviction,
    record_fallback,
    record_fetch_duration,
)

if TYPE_CHECKING:
    from ..channel_history_fetcher import ChannelHistoryFetcher

logger = logging.getLogger(__name__)

_WindowCacheKey = tuple[str, int, str]
_WindowCacheValue = tuple[str, list[dict[str, Any]]]

# RFC 0034 Phase 3: the cache is bounded. Phase 1 shipped an unbounded dict —
# a channel seen once kept its tuple for the life of the process, so the dict
# grew with the number of *distinct* ``(channel, limit, agent)`` triples ever
# served (not the number concurrently active), each holding up to
# ``max_turns + 1`` raw rows. Harmless for a handful of dogfood channels;
# unbounded over a long-lived orchestrator across many channels (PR plan
# §Future Phases). The bound is a process-global constant — a one-line retune,
# like ``DEFAULT_MAX_TURNS`` — and ``conversation_window.cache_evictions``
# telemetry now makes an undersized bound visible (thrashing) rather than a
# guess. It is generous: each entry is small (≤ ``max_turns + 1`` row dicts)
# and a single-node orchestrator serves far fewer than this many live channels.
DEFAULT_WINDOW_CACHE_CAPACITY: int = 256


class _WindowCache:
    """A bounded least-recently-used cache for the per-turn fetch (RFC §F).

    Both a :meth:`get` hit and a :meth:`put` mark an entry most-recently-used;
    on overflow the least-recently-used entry is evicted. An in-place update of
    an existing key (the §F message-id re-stamp) refreshes recency without
    growing the cache, so a hot channel is never evicted by its own
    invalidation. Eviction is reported by :meth:`put`'s return value so the
    call site can chart ``conversation_window.cache_evictions`` without the
    cache depending on the observability layer.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            # A zero/negative bound would evict every fresh insert immediately —
            # a "cache that never caches", strictly worse than the unbounded
            # dict it replaces. Fail loud rather than silently disable caching.
            msg = f"window cache capacity must be >= 1, got {capacity}"
            raise ValueError(msg)
        self._capacity = capacity
        self._data: OrderedDict[_WindowCacheKey, _WindowCacheValue] = OrderedDict()

    @property
    def capacity(self) -> int:
        return self._capacity

    def get(self, key: _WindowCacheKey) -> _WindowCacheValue | None:
        """Return the cached value and mark it most-recently-used, or ``None``."""
        value = self._data.get(key)
        if value is None:
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: _WindowCacheKey, value: _WindowCacheValue) -> int:
        """Insert/update ``key`` and return the number of entries evicted (0/1)."""
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self._capacity:
            self._data.popitem(last=False)
            return 1
        return 0

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_WINDOW_CACHE = _WindowCache(DEFAULT_WINDOW_CACHE_CAPACITY)


async def _fetch_window(
    *,
    history_fetcher: ChannelHistoryFetcher,
    channel_id: str,
    message_id: str | None,
    limit: int,
    agent_id: str,
) -> list[dict[str, Any]] | None:
    """Return the raw channel-history rows, or ``None`` on fetch failure.

    Scoped to ``agent_id`` via the fetch's ``as_participant`` (RFC 0036 §G),
    so ``agent_id`` keys the cache too; pass a real id — a falsy ``agent_id``
    silently fetches UNSCOPED (a falsy ``as_participant`` is omitted). A
    Protocol exception degrades to ``None`` with a WARN; a ``None`` from the
    fetcher is its own already-logged best-effort failure, degrading silently.

    RFC 0034 Phase 3 telemetry rides the real branches here: a consulted
    look-up charts ``conversation_window.cache_access`` (hit/miss), a real fetch
    charts its latency on ``conversation_window.fetch_duration``, an LRU
    eviction charts ``conversation_window.cache_evictions``, and either
    degrade-to-``None`` path charts ``conversation_window.fallback`` so the
    otherwise-silent degradation (§F risk table) is observable.
    """
    cache_key = (channel_id, limit, agent_id)
    if message_id is not None:
        cached = _WINDOW_CACHE.get(cache_key)
        if cached is not None and cached[0] == message_id:
            record_cache_access(hit=True)
            return cached[1]
        record_cache_access(hit=False)

    start = time.perf_counter()
    try:
        raw = await history_fetcher.fetch(channel_id, limit=limit, as_participant=agent_id)
    except Exception as exc:
        logger.warning(
            "conversation window: history fetch raised for channel %s: %s",
            channel_id,
            exc,
            extra={
                "reason": "conversation_window_fetch_failed",
                "channel_id": channel_id,
            },
        )
        record_fallback(reason="fetch_failed")
        return None
    record_fetch_duration((time.perf_counter() - start) * 1000.0)

    if raw is None:
        record_fallback(reason="fetch_none")
        return None

    if message_id is not None:
        evicted = _WINDOW_CACHE.put(cache_key, (message_id, raw))
        if evicted:
            record_cache_eviction(evicted)
    return raw
