"""RFC 0034 Phase 3 — bounded LRU for the conversation-window fetch cache.

Phase 1 shipped the in-process fetch cache as an unbounded dict: a channel
seen once kept its ``(message_id, raw_rows)`` tuple for the life of the
process, so the dict grew with the number of *distinct* ``(channel, limit,
agent)`` triples a long-running orchestrator ever served (RFC 0034 PR plan
§Future Phases). Phase 3 caps it at :data:`DEFAULT_WINDOW_CACHE_CAPACITY`
entries, evicting the least-recently-used on overflow.

This pins the data-structure contract of :class:`_WindowCache` in isolation —
the metric emit at its call sites is pinned separately by
``test_conversation_window_metrics.py``. Both a get-hit and an insert mark an
entry most-recently-used; an update of an existing key never grows the cache.
"""

from __future__ import annotations

import pytest

from agents.persona_runtime._conversation_window_cache import (
    DEFAULT_WINDOW_CACHE_CAPACITY,
    _WindowCache,
)

_ROWS: list[dict[str, object]] = [{"id": "m1", "sender_id": "user", "content": "hi"}]


def _val(message_id: str) -> tuple[str, list[dict[str, object]]]:
    return (message_id, _ROWS)


class TestWindowCacheBasics:
    def test_put_then_get_round_trips(self) -> None:
        cache = _WindowCache(capacity=4)
        cache.put(("c", 21, "a"), _val("m1"))
        assert cache.get(("c", 21, "a")) == _val("m1")

    def test_get_missing_key_returns_none(self) -> None:
        cache = _WindowCache(capacity=4)
        assert cache.get(("c", 21, "a")) is None

    def test_clear_empties_the_cache(self) -> None:
        cache = _WindowCache(capacity=4)
        cache.put(("c", 21, "a"), _val("m1"))
        cache.clear()
        assert len(cache) == 0
        assert cache.get(("c", 21, "a")) is None

    def test_capacity_below_one_is_rejected(self) -> None:
        # A zero/negative bound would make the cache evict its own fresh
        # insert on every put — a silent "cache that never caches", worse
        # than the unbounded dict it replaces. Fail loud at construction.
        with pytest.raises(ValueError):
            _WindowCache(capacity=0)

    def test_default_capacity_is_a_positive_bound(self) -> None:
        assert isinstance(DEFAULT_WINDOW_CACHE_CAPACITY, int)
        assert DEFAULT_WINDOW_CACHE_CAPACITY >= 1


class TestWindowCacheEviction:
    def test_insert_over_capacity_evicts_least_recently_used(self) -> None:
        cache = _WindowCache(capacity=2)
        cache.put(("a", 21, "x"), _val("m-a"))
        cache.put(("b", 21, "x"), _val("m-b"))
        # 'a' is the oldest; inserting a third entry evicts it.
        cache.put(("c", 21, "x"), _val("m-c"))
        assert cache.get(("a", 21, "x")) is None
        assert cache.get(("b", 21, "x")) == _val("m-b")
        assert cache.get(("c", 21, "x")) == _val("m-c")
        assert len(cache) == 2

    def test_put_returns_number_evicted(self) -> None:
        cache = _WindowCache(capacity=1)
        assert cache.put(("a", 21, "x"), _val("m-a")) == 0
        # The second distinct key overflows the bound and evicts one entry.
        assert cache.put(("b", 21, "x"), _val("m-b")) == 1

    def test_get_marks_entry_most_recently_used(self) -> None:
        # LRU, not FIFO: touching 'a' with a get must spare it from the next
        # eviction even though it was inserted first.
        cache = _WindowCache(capacity=2)
        cache.put(("a", 21, "x"), _val("m-a"))
        cache.put(("b", 21, "x"), _val("m-b"))
        assert cache.get(("a", 21, "x")) == _val("m-a")  # 'a' now most-recent
        cache.put(("c", 21, "x"), _val("m-c"))  # evicts 'b' (now oldest)
        assert cache.get(("b", 21, "x")) is None
        assert cache.get(("a", 21, "x")) == _val("m-a")
        assert cache.get(("c", 21, "x")) == _val("m-c")

    def test_update_existing_key_neither_grows_nor_evicts(self) -> None:
        cache = _WindowCache(capacity=2)
        cache.put(("a", 21, "x"), _val("m-a"))
        cache.put(("b", 21, "x"), _val("m-b"))
        # Re-stamping 'a' with a newer message id is the §F invalidation —
        # an in-place update, not growth, so nothing is evicted.
        assert cache.put(("a", 21, "x"), _val("m-a2")) == 0
        assert len(cache) == 2
        assert cache.get(("a", 21, "x")) == _val("m-a2")
        assert cache.get(("b", 21, "x")) == _val("m-b")

    def test_update_existing_key_refreshes_recency(self) -> None:
        # The §F re-stamp must also count as a use: after updating 'a', the
        # oldest entry is 'b', so the next overflow evicts 'b', not 'a'.
        cache = _WindowCache(capacity=2)
        cache.put(("a", 21, "x"), _val("m-a"))
        cache.put(("b", 21, "x"), _val("m-b"))
        cache.put(("a", 21, "x"), _val("m-a2"))  # 'a' now most-recent
        cache.put(("c", 21, "x"), _val("m-c"))  # evicts 'b'
        assert cache.get(("b", 21, "x")) is None
        assert cache.get(("a", 21, "x")) == _val("m-a2")
