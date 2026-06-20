"""RFC 0036 PR 5 (Phase 3) — the conversation window's membership scope.

PR 5 makes the live persona prompt obey the same membership scope as
verbatim recall: :func:`build_conversation_messages` passes the persona's
``agent_id`` as the history fetch's ``as_participant``, so the server
(via :meth:`channels.ChannelStore.GetHistoryScoped`) trims the window to
the persona's membership stints — a re-added persona's live window no
longer shows its removal-gap messages.

Two contracts pinned here:

* **The fetch is scoped.** ``as_participant`` reaching the fetcher equals
  the persona's ``agent_id`` — the window is scoped to *the persona*, and
  the LLM cannot influence the scope subject (it is the runtime's own id).
* **The cache key includes ``agent_id``.** Because the fetched rows are
  now membership-scoped per persona, two personas reacting to the *same*
  message on the *same* channel at the *same* fetch limit must NOT share a
  cache entry — each gets its own scoped window. This is the load-bearing
  cache-key correctness fix (omitting ``agent_id`` would serve one
  persona's window to another), while the same-persona retry path still
  hits the cache.

The store-level no-op-for-current-member proof lives in
``internal/channels/sqlite_history_scoped_test.go``; here the window's job
is only to *pass the scope* and *key the cache by it*.
"""

from __future__ import annotations

import pytest

from agents.persona_runtime import conversation_window

from ._conversation_window_test_helpers import (
    _AGENT_ID,
    _build,
    _event,
    _FakeChannelHistoryFetcher,
    _row,
)


@pytest.fixture(autouse=True)
def _clear_window_cache():
    """The conversation-window cache is module-level (RFC 0034 §F); clear
    it around every test so cache-hit / cache-miss cases do not bleed."""
    conversation_window._WINDOW_CACHE.clear()
    yield
    conversation_window._WINDOW_CACHE.clear()


class TestWindowMembershipScope:
    async def test_fetch_receives_agent_id_as_as_participant(self):
        """The window fetch carries ``as_participant == agent_id`` (RFC 0036
        §G) so the server scopes the history to the persona's stints. The
        scope subject is the runtime's own id, never an LLM-supplied value."""
        fetcher = _FakeChannelHistoryFetcher([_row("m1", "user", "hello")])
        await _build(fetcher, agent_id=_AGENT_ID)
        assert fetcher.calls, "the window issued a fetch"
        # calls record (channel_id, limit, as_participant).
        assert fetcher.calls[-1][2] == _AGENT_ID, (
            f"as_participant must equal the persona agent_id; "
            f"got {fetcher.calls!r}"
        )

    async def test_distinct_agents_do_not_share_cache_entry(self):
        """Two personas reacting to the SAME message on the SAME channel at
        the SAME fetch limit must each get their OWN membership-scoped
        window — the rows are now per-persona, so ``agent_id`` is in the
        cache key. Omitting it would serve persona A's (possibly
        gap-trimmed) rows to persona B.
        """
        event = _event(message_id="m-shared")
        # A real scoped endpoint returns different rows per participant; the
        # fake models that with a per-call sequence.
        fetcher = _FakeChannelHistoryFetcher(
            results=[
                [_row("m1", "user", "window for alice")],
                [_row("m2", "user", "window for bob")],
            ],
        )
        alice = await _build(fetcher, event=event, agent_id="alice")
        bob = await _build(fetcher, event=event, agent_id="bob")

        # Both fetched — bob is a cache MISS despite the identical
        # (channel, message_id, limit), because agent_id differs.
        assert len(fetcher.calls) == 2
        assert fetcher.calls[0][2] == "alice"
        assert fetcher.calls[1][2] == "bob"
        assert "window for alice" in alice[0]["content"]
        assert "window for bob" in bob[0]["content"]
        assert "window for bob" not in "".join(m["content"] for m in alice)

    async def test_same_agent_repeat_still_hits_cache(self):
        """The §F retry / sub-agent-return optimization survives the
        ``agent_id`` cache-key change: the SAME persona reacting to the same
        ``(channel_id, message_id)`` at the same limit issues one fetch."""
        event = _event(message_id="m-same")
        fetcher = _FakeChannelHistoryFetcher([_row("m1", "user", "cached")])
        first = await _build(fetcher, event=event, agent_id=_AGENT_ID)
        second = await _build(fetcher, event=event, agent_id=_AGENT_ID)
        assert len(fetcher.calls) == 1
        assert first == second
