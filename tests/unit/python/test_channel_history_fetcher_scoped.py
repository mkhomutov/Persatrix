"""RFC 0036 PR 5 (Phase 3) — the history fetcher's ``as_participant`` scope.

Sibling of ``test_channel_history_fetcher.py`` (kept separate so neither
file passes the 500-line review cap). Pins the one thing PR 5 adds to
:meth:`agents.channel_history_fetcher.HttpChannelHistoryFetcher.fetch`:
the optional ``as_participant`` argument, which — when set — rides the
query string as ``&as_participant=`` so the server scopes the history to
that participant's membership stints (RFC 0036 §G), and — when absent —
is omitted entirely so human / CLI callers get the full unscoped history
(OQ #4: the param is the only switch, no operator opt-in).

The shared ``orchestrator`` loopback fixture (registered via conftest.py)
records each request's ``path_qs``, so these tests pin the exact query
string the fetcher sends.
"""

from __future__ import annotations

import aiohttp

from agents.channel_history_fetcher import HttpChannelHistoryFetcher


class TestHistoryFetcherAsParticipant:
    async def test_fetch_appends_as_participant_when_set(self, orchestrator):
        """When ``as_participant`` is supplied — the persona conversation-
        window / catch-up path — it rides the query string as
        ``&as_participant=`` so the server scopes the history to that
        participant's membership stints (a re-added persona's window then
        excludes its removal-gap messages)."""
        base_url, state = orchestrator
        state["history"]["c1"] = []

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            await fetcher.fetch("c1", limit=20, as_participant="ember-owl")

        assert any(
            p == "/api/v1/channels/c1/messages?limit=20&as_participant=ember-owl"
            for p in state["log"]
        ), f"expected &as_participant=ember-owl; got {state['log']!r}"

    async def test_fetch_omits_as_participant_when_absent(self, orchestrator):
        """The human / CLI path leaves ``as_participant`` at its ``None``
        default; the query string carries no such param, so the server
        returns the full unscoped history exactly as before (OQ #4)."""
        base_url, state = orchestrator
        state["history"]["c1"] = []

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            await fetcher.fetch("c1", limit=20)

        assert any(
            p == "/api/v1/channels/c1/messages?limit=20" for p in state["log"]
        ), f"expected the bare ?limit= URL; got {state['log']!r}"
        assert all("as_participant" not in p for p in state["log"]), \
            f"unscoped fetch must not send as_participant; got {state['log']!r}"

    async def test_fetch_url_encodes_as_participant(self, orchestrator):
        """``as_participant`` is percent-encoded into a single query value
        (``quote(..., safe='')``) so a reserved character can never splice an
        extra query parameter or widen the request — defense-in-depth, even
        though RFC 0011 participant ids are constrained."""
        base_url, state = orchestrator
        state["history"]["c1"] = []

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            await fetcher.fetch("c1", limit=5, as_participant="a&b=c")

        assert any("as_participant=a%26b%3Dc" in p for p in state["log"]), \
            f"expected percent-encoded as_participant; got {state['log']!r}"


# pytest-asyncio auto-detects ``async def`` tests via ``asyncio_mode = "auto"``.
