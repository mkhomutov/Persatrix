"""RFC 0034 Phase 1 PR 2 — conversation-window deep-review follow-ups.

Pins the ordering guarantee added in response to the PR 2 deep-review:
a message persisted to the channel store *after* the current event but
*before* the window fetch must not be replayed as a turn ahead of the
current event (RFC §B). Kept separate from ``test_conversation_window.py``
so neither file blows past the 500-line review cap; both share the input
builders in ``_conversation_window_test_helpers.py``.
"""

from __future__ import annotations

import pytest

from agents.persona_runtime import conversation_window

from ._conversation_window_test_helpers import (
    _AGENT_ID,
    _CURRENT,
    _FakeChannelHistoryFetcher,
    _build,
    _event,
    _row,
)


@pytest.fixture(autouse=True)
def _clear_window_cache():
    """The conversation-window cache is module-level (RFC §F); clear it
    around every test so cache-hit / cache-miss cases do not bleed."""
    conversation_window._WINDOW_CACHE.clear()
    yield
    conversation_window._WINDOW_CACHE.clear()


# ─── Ordering vs. concurrent writers (RFC §B) ──────────────


class TestOrderingAgainstConcurrentWrites:
    """The replayed transcript must never place a message newer than the
    current event ahead of it. The orchestrator persists channel messages
    independently of the persona's per-agent event lock, so a message that
    arrives after the current event can land in the channel store before
    this window's history fetch runs — the fetch reflects the store, not
    the persona's event queue."""

    async def test_message_newer_than_current_event_is_not_replayed_ahead(self):
        """A peer message persisted after the current event but before the
        window fetch is dropped — replaying it would show the model a
        future message ahead of the turn it is answering."""
        event = _event(message_id="m-current")
        # Newest-first, as the endpoint returns it: m-newer landed after
        # the current event; then the current event; then an older line.
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m-newer", "user", "a line that arrived after the current turn"),
                _row("m-current", "user", "current message body"),
                _row("m-older", "user", "an earlier line"),
            ],
        )
        result = await _build(fetcher, event=event)
        joined = " ".join(m["content"] for m in result)
        assert "arrived after the current turn" not in joined
        # The older line still replays; the current event is appended last.
        assert "an earlier line" in result[0]["content"]
        assert result[-1] == {"role": "user", "content": _CURRENT}
        assert len(result) == 2

    async def test_every_row_newer_than_current_event_is_dropped(self):
        """The whole newer prefix is dropped, not just the first row — the
        current event's row anchors the cut."""
        event = _event(message_id="m-current")
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m-newer-2", "user", "second line after the current turn"),
                _row("m-newer-1", "user", "first line after the current turn"),
                _row("m-current", "user", "current message body"),
                _row("m-older", _AGENT_ID, "an earlier persona line"),
            ],
        )
        result = await _build(fetcher, event=event)
        joined = " ".join(m["content"] for m in result)
        assert "after the current turn" not in joined
        assert result[0] == {
            "role": "assistant",
            "content": "an earlier persona line",
        }
        assert len(result) == 2

    async def test_unpersisted_current_event_replays_every_row(self):
        """When the current event is not yet in the channel store there is
        no ordering anchor and every fetched row replays. Channel
        persistence is FIFO, so an absent current event implies no
        strictly-newer row is persisted either."""
        event = _event(message_id="m-current")
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m2", _AGENT_ID, "a persona line"),
                _row("m1", "user", "a peer line"),
            ],
        )
        result = await _build(fetcher, event=event)
        # Neither row carries the current event's id — both replay.
        assert len(result) == 3
        assert "a peer line" in result[0]["content"]
        assert result[1] == {"role": "assistant", "content": "a persona line"}
        assert result[-1] == {"role": "user", "content": _CURRENT}
