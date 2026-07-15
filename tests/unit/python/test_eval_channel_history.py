"""Unit tests for the RFC 0044 in-process channel-history fetcher.

:class:`~evaluators.eval_channel_history.InProcessChannelHistory` is the seam that
lets the eval driver drive the real RFC 0034 conversation window (working memory):
the driver appends each delivered turn, and the persona runtime fetches the window
during prompt assembly. The window reads ``id`` / ``sender_id`` / ``content`` and
expects newest-first ordering (``agents/persona_runtime/conversation_window.py``),
so these tests pin that contract exactly.
"""

from __future__ import annotations

from evaluators.eval_channel_history import InProcessChannelHistory

_CH = "dm:sam:ember-owl"


async def test_fetch_unknown_channel_is_empty_not_none() -> None:
    # The window branches on None (best-effort failure) vs [] (empty). An
    # in-memory log has no failure mode, so an unknown channel is [] — never None.
    hist = InProcessChannelHistory()
    assert await hist.fetch("no-such-channel", limit=10) == []


async def test_append_then_fetch_returns_newest_first_in_row_shape() -> None:
    hist = InProcessChannelHistory()
    hist.append(channel_id=_CH, message_id="m0", sender_id="sam", content="hello")
    hist.append(channel_id=_CH, message_id="m1", sender_id="ember-owl", content="hi there")

    rows = await hist.fetch(_CH, limit=10)

    # Newest-first (RFC 0011 §C) — the window reverses it back to chronological.
    assert rows == [
        {"id": "m1", "sender_id": "ember-owl", "content": "hi there"},
        {"id": "m0", "sender_id": "sam", "content": "hello"},
    ]


async def test_fetch_caps_to_newest_limit() -> None:
    hist = InProcessChannelHistory()
    for n in range(5):
        hist.append(channel_id=_CH, message_id=f"m{n}", sender_id="sam", content=f"t{n}")

    rows = await hist.fetch(_CH, limit=2)

    # The two NEWEST rows, newest-first — not the two oldest.
    assert [r["id"] for r in rows] == ["m4", "m3"]


async def test_channels_are_isolated() -> None:
    hist = InProcessChannelHistory()
    hist.append(channel_id="ch-a", message_id="a0", sender_id="sam", content="in a")
    hist.append(channel_id="ch-b", message_id="b0", sender_id="sam", content="in b")

    assert [r["id"] for r in await hist.fetch("ch-a", limit=10)] == ["a0"]
    assert [r["id"] for r in await hist.fetch("ch-b", limit=10)] == ["b0"]


async def test_as_participant_is_accepted_and_ignored() -> None:
    # RFC 0036 §G membership scoping is a no-op here (single always-present
    # persona), but the param must be accepted for Protocol conformance.
    hist = InProcessChannelHistory()
    hist.append(channel_id=_CH, message_id="m0", sender_id="sam", content="x")

    scoped = await hist.fetch(_CH, limit=10, as_participant="ember-owl")
    unscoped = await hist.fetch(_CH, limit=10)
    assert scoped == unscoped


def test_conforms_to_channel_history_fetcher_protocol() -> None:
    # Structural conformance to the seam `set_history_fetcher` accepts — the whole
    # point of the class. A signature drift here would fail silently at wiring.
    from agents.channel_history_fetcher import ChannelHistoryFetcher

    fetcher: ChannelHistoryFetcher = InProcessChannelHistory()
    assert fetcher is not None
