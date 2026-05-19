"""RFC 0034 Phase 1 — conversation-window deep-review follow-ups.

Pins guarantees added in response to the conversation-window deep
reviews. Kept separate from ``test_conversation_window.py`` so neither
file blows past the 500-line review cap; both share the input builders
in ``_conversation_window_test_helpers.py``.

* **PR 2 — ordering vs. concurrent writers (RFC §B).** A message
  persisted to the channel store *after* the current event but *before*
  the window fetch must not be replayed as a turn ahead of the current
  event (:class:`TestOrderingAgainstConcurrentWrites`).
* **PR 3 — leading-role guard.** The reconstructed ``messages`` array
  must never open with an ``assistant`` turn — the Anthropic Messages
  API requires ``messages[0]`` to use the ``user`` role
  (:class:`TestLeadingAssistantTurnGuard`).
"""

from __future__ import annotations

import pytest

from agents.persona_runtime import conversation_window
from agents.persona_runtime.conversation_window import ConversationWindowConfig

from ._conversation_window_test_helpers import (
    _AGENT_ID,
    _CURRENT,
    _build,
    _event,
    _FakeChannelHistoryFetcher,
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
                _row("m-older", "user", "an earlier peer line"),
            ],
        )
        result = await _build(fetcher, event=event)
        joined = " ".join(m["content"] for m in result)
        assert "after the current turn" not in joined
        assert result[0]["role"] == "user"
        assert "an earlier peer line" in result[0]["content"]
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


# ─── Leading-role guard (RFC §C / Anthropic messages[0]) ───


class TestLeadingAssistantTurnGuard:
    """``build_conversation_messages`` returns ``[*replayed, current_turn]``;
    ``current_turn`` is always a ``user`` turn but ``replayed`` is not
    guaranteed to *start* with one. The role split maps the persona's own
    messages to ``role="assistant"``, so a persona-first channel — or
    token-FIFO admission evicting an odd number of leading turns — can
    leave the transcript opening with an ``assistant`` turn. The Anthropic
    Messages API rejects an ``assistant``-leading ``messages`` array with
    a hard 400 (``messages[0]`` must use the ``user`` role) and the
    persona runtime passes the seed through unmodified, so the window
    drops any leading ``assistant`` prefix as the final admission step."""

    async def test_persona_first_channel_drops_leading_assistant_turn(self):
        """A channel the persona opened — a greeting or proactive turn —
        replays ``assistant``-first; the dangling opening turn is dropped
        so the seed still starts with a ``user`` turn."""
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m2", "user", "a peer reply"),
                _row("m1", _AGENT_ID, "a persona greeting"),
            ],
        )
        result = await _build(fetcher)
        assert result[0]["role"] == "user"
        joined = " ".join(m["content"] for m in result)
        assert "a persona greeting" not in joined
        assert "a peer reply" in result[0]["content"]
        assert result[-1] == {"role": "user", "content": _CURRENT}
        assert len(result) == 2

    async def test_token_fifo_trim_to_leading_assistant_is_dropped(self):
        """Token-overflow FIFO can evict the oldest ``user`` turn and
        leave an ``assistant`` turn at the front; that dangling turn is
        dropped after admission so the seed still opens with ``user``."""
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m3", "user", "a small peer line"),
                _row("m2", _AGENT_ID, "a small persona line"),
                _row("m1", "user", "x" * 4000),
            ],
        )
        result = await _build(
            fetcher, config=ConversationWindowConfig(max_tokens=200),
        )
        assert result[0]["role"] == "user"
        joined = " ".join(m["content"] for m in result)
        # The oversize oldest turn is evicted by token-FIFO ...
        assert "x" * 4000 not in joined
        # ... which leaves the persona turn leading — it is then dropped.
        assert "a small persona line" not in joined
        assert "a small peer line" in result[0]["content"]
        assert result[-1] == {"role": "user", "content": _CURRENT}
        assert len(result) == 2

    async def test_all_persona_transcript_degrades_to_current_event_only(self):
        """A channel where only the persona has spoken replays as an
        all-``assistant`` transcript; dropping every leading ``assistant``
        turn empties it and the seed degrades to the current event alone
        — identical to an empty channel or a fetch failure."""
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m2", _AGENT_ID, "a later persona line"),
                _row("m1", _AGENT_ID, "an earlier persona line"),
            ],
        )
        result = await _build(fetcher)
        assert result == [{"role": "user", "content": _CURRENT}]

    async def test_non_leading_assistant_turn_is_preserved(self):
        """The guard strips only a *leading* ``assistant`` prefix — a
        transcript already opening with a ``user`` turn is untouched and
        an ``assistant`` turn in a non-leading position survives."""
        fetcher = _FakeChannelHistoryFetcher(
            [
                _row("m3", "user", "a later peer line"),
                _row("m2", _AGENT_ID, "a persona line"),
                _row("m1", "user", "an earlier peer line"),
            ],
        )
        result = await _build(fetcher)
        assert "an earlier peer line" in result[0]["content"]
        assert result[1] == {"role": "assistant", "content": "a persona line"}
        assert "a later peer line" in result[2]["content"]
        assert result[-1] == {"role": "user", "content": _CURRENT}
        assert len(result) == 4
