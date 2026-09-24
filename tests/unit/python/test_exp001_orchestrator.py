"""EXP-001 harness — what the harness asks the orchestrator, and reads in its log (PR 5a).

The harness drives each channel meeting over the orchestrator's REST API, as
an operator would. Nothing on that API says when a discussion has closed,
so the harness also reads the orchestrator's own log: each close and its
trigger, each discussion cut at the depth cap, every lease the wallet
refused (check 6), and the startup line that says the rate limiter is off.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from evaluators.exp001.orchestrator import (
    _BOUNDED_CLOSE,
    _DEPTH_CAP,
    _END_VOTE_CLOSE,
    _RATE_LIMIT_OFF,
    _REFUSED_LEASE,
    _SPENDING_LIMIT_REFUSAL,
    _WALLET_SERVED,
    HARNESS_ID,
    Close,
    Message,
    Orchestrator,
    OrchestratorError,
    read_orchestrator_log,
)

_REPO = Path(__file__).resolve().parents[3]


def _line(message: str, **fields: Any) -> str:
    return json.dumps({
        "schema_version": "1", "timestamp": "2026-09-24T09:53:53.214935Z", "level": "INFO",
        "message": message, **fields,
    })


class TestReadOrchestratorLog:
    def test_it_finds_each_close_with_its_channel_and_trigger(self, tmp_path: Path) -> None:
        log = tmp_path / "orchestrator.log"
        log.write_text("\n".join([
            _line("security.rate_limit.disabled scope=startup", source="SECURITY_RATE_LIMIT_"),
            _line(
                "channels: interaction closed by end-of-interaction votes",
                channel_id="group:advice-1", interaction_id="i-1", participant_id="ripple-kite",
                votes=4, trigger="end_votes",
            ),
            "panic: a line that is not JSON",
            _line(
                "channels: autonomous discussion reached the cascade-depth cap; running the "
                "structural close", channel_id="group:advice-2", interaction_id="i-2",
            ),
            _line(
                "channels: interaction closed by RFC 0052 bounded close",
                channel_id="group:advice-2", interaction_id="i-2", trigger="structural",
            ),
            _line("wallet lease enforcement initialized", leaseTTL="5m0s"),
            _line("gRPC server listening", addr="127.0.0.1:19090"),
            "",
        ]))
        read = read_orchestrator_log(log)
        at = dt.datetime(2026, 9, 24, 9, 53, 53, 214935, tzinfo=dt.UTC)
        assert read.closes == (
            Close(channel="group:advice-1", interaction="i-1", trigger="end_votes", at=at),
            Close(channel="group:advice-2", interaction="i-2", trigger="structural", at=at),
        )
        assert read.depth_capped == frozenset({"i-2"})
        assert read.refused_leases == ()
        assert read.rate_limit_off
        assert read.wallet_served

    def test_the_wallet_is_served_only_once_both_its_startup_lines_are_logged(
        self, tmp_path: Path,
    ) -> None:
        """Without the log buffer the orchestrator starts no gRPC server, so
        every adviser's model call is refused before any lease is asked for."""
        log = tmp_path / "orchestrator.log"
        log.write_text(_line("wallet lease enforcement initialized"))
        assert not read_orchestrator_log(log).wallet_served

    def test_it_tells_a_spending_limit_refusal_from_every_other(self, tmp_path: Path) -> None:
        """Check 6 is about the three spending limits alone; any other refusal
        is a failure the system causes."""
        log = tmp_path / "orchestrator.log"
        log.write_text("\n".join([
            _line("wallet: lease denied — budget exceeded", agent_id="velvet-pika",
                  scope="per_agent"),
            _line("wallet: lease denied — interaction cost ceiling exceeded",
                  agent_id="lunar-stoat"),
            _line("wallet: request rejected — token count out of range", agent_id="ripple-kite"),
            _line("wallet: lease granted", agent_id="ripple-kite"),
        ]))
        read = read_orchestrator_log(log)
        assert read.spending_limit_refusals == (
            "velvet-pika: wallet: lease denied — budget exceeded",
        )
        assert read.refused_leases == (
            "lunar-stoat: wallet: lease denied — interaction cost ceiling exceeded",
            "ripple-kite: wallet: request rejected — token count out of range",
        )

    def test_a_log_not_yet_written_holds_nothing(self, tmp_path: Path) -> None:
        read = read_orchestrator_log(tmp_path / "orchestrator.log")
        assert (
            read.closes, read.spending_limit_refusals, read.refused_leases,
            read.rate_limit_off, read.wallet_served,
        ) == ((), (), (), False, False)

    def test_the_closes_of_one_channel(self, tmp_path: Path) -> None:
        log = tmp_path / "orchestrator.log"
        log.write_text("\n".join(
            _line("channels: interaction closed by RFC 0052 bounded close",
                  channel_id=channel, interaction_id=f"i-{n}", trigger="structural")
            for n, channel in enumerate(["group:a", "group:b", "group:a"])
        ))
        assert [c.interaction for c in read_orchestrator_log(log).closes_in("group:a")] == [
            "i-0", "i-2",
        ]


def _logged(go: str, message: str) -> str:
    """The rest of the Go logger call that logs *message*: the fields it logs."""
    source = (_REPO / go).read_text()
    start = source.index(f'"{message}"')
    depth = 1
    for end in range(start, len(source)):
        depth += {"(": 1, ")": -1}.get(source[end], 0)
        if depth == 0:
            return source[start:end]
    raise AssertionError(f"{go}: the call that logs {message!r} never closes")


class TestTheLinesTheHarnessReads:
    """The orchestrator's own source logs every line the harness reads, with
    every field it reads, so a reworded line fails here instead of turning a
    close into an idle one or hiding a refused lease."""

    @pytest.mark.parametrize(("go", "message", "fields"), [
        ("internal/channels/end_vote.go", _END_VOTE_CLOSE,
         ("channel_id", "interaction_id", "trigger")),
        ("internal/channels/bounded_close.go", _BOUNDED_CLOSE,
         ("channel_id", "interaction_id", "trigger")),
        ("internal/channels/autonomous_continuation.go", _DEPTH_CAP,
         ("channel_id", "interaction_id")),
        ("internal/wallet/wallet.go", _SPENDING_LIMIT_REFUSAL, ("agent_id",)),
        ("cmd/orchestrator/ratelimit.go", _RATE_LIMIT_OFF, ()),
        *(("cmd/orchestrator/main.go", line, ()) for line in sorted(_WALLET_SERVED)),
    ])
    def test_each_line_is_logged_with_the_fields_read(
        self, go: str, message: str, fields: tuple[str, ...],
    ) -> None:
        call = _logged(go, message)
        for name in fields:
            assert f'zap.String("{name}",' in call, f"{go} logs {message!r} without {name}"

    @pytest.mark.parametrize(("go", "message"), [
        ("internal/wallet/wallet.go", "wallet: lease denied — per-agent active-lease cap reached"),
        ("internal/wallet/interaction_budget.go",
         "wallet: lease denied — interaction cost ceiling exceeded"),
        ("internal/wallet/validation.go", "wallet: request rejected — token count out of range"),
    ])
    def test_every_other_refusal_starts_as_the_harness_expects(self, go: str, message: str) -> None:
        assert message.startswith(_REFUSED_LEASE)
        assert _logged(go, message)


_METADATA = {
    "interaction_id": "i-3", "participant_type": "user",
    "previous_interaction_id": "i-2", "previous_interaction_close_trigger": "structural",
}
_ECHO: dict[str, Any] = {
    "id": "m-9", "channel_id": "group:advice-1", "sender_id": "operator",
    "content": "The discussion has ended.", "timestamp": "2026-09-24T09:55:47.123456789Z",
    "mentions": ["lunar-stoat"], "metadata": _METADATA,
}


class _Orchestrator:
    """A stand-in for the orchestrator's REST API that records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.revision = 3
        self.fail: int | None = None
        self.stall = 0.0  # seconds to wait before answering
        self.garbage = False  # answer 200 with a body that is not JSON
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.server = TestServer(app, host="127.0.0.1")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path_qs, dict(request.headers), body))
        await asyncio.sleep(self.stall)
        if self.fail is not None:
            return web.json_response({"error": {"message": "no such channel"}}, status=self.fail)
        if self.garbage:
            return web.Response(text="<html>proxy error</html>")
        route = (request.method, request.path)
        if route == ("GET", "/healthz"):
            return web.Response(text="ok")
        if route == ("GET", "/api/v1/agents"):
            return web.json_response([{"id": "lunar-stoat"}, {"id": "velvet-pika"}])
        if route == ("POST", "/api/v1/channels/group:advice-1/messages"):
            return web.json_response(_ECHO, status=201)
        if route == ("GET", "/api/v1/channels/group:advice-1/messages"):
            newest_first = [
                {**_ECHO, "id": "m-2", "sender_id": "lunar-stoat",
                 "timestamp": "2026-09-24T09:55:48Z", "metadata": {}},
                {**_ECHO, "id": "m-1", "metadata": None},
            ]
            return web.json_response({"messages": newest_first, "classification": "internal"})
        if route == ("GET", "/api/v1/channels/group:advice-1/config"):
            return web.json_response({"revision": self.revision})
        if route == ("PATCH", "/api/v1/channels/group:advice-1/config"):
            return web.json_response({"revision": self.revision + 1})
        if route == ("PATCH", "/api/v1/channels/group:advice-1/members/velvet-pika"):
            return web.Response(status=204)
        if route == ("GET", "/api/v1/channels/group:advice-1/activity"):
            return web.json_response({"thinking": ["ripple-kite"]})
        return web.json_response({"error": "unrouted"}, status=500)


@pytest.fixture
async def fake() -> AsyncIterator[_Orchestrator]:
    orchestrator = _Orchestrator()
    await orchestrator.server.start_server()
    try:
        yield orchestrator
    finally:
        await orchestrator.server.close()


@pytest.fixture
async def client(fake: _Orchestrator) -> AsyncIterator[Orchestrator]:
    async with aiohttp.ClientSession() as session:
        yield Orchestrator(str(fake.server.make_url("")), session)


class TestTheRestClient:
    async def test_every_request_carries_the_harness_id(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        assert await client.healthy()
        assert await client.agents() == {"lunar-stoat", "velvet-pika"}
        assert [r[2].get("X-Agent-ID") for r in fake.requests] == [HARNESS_ID] * 2

    async def test_the_operator_posts_word_for_word(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        text = "Today is Monday 13 October 2036.\n\nPlan to critique: a gala."
        echo = await client.post("group:advice-1", "operator", text, mentions=["lunar-stoat"])
        [(method, path, _, body)] = fake.requests
        assert (method, path) == ("POST", "/api/v1/channels/group:advice-1/messages")
        assert body == {"sender_id": "operator", "content": text, "mentions": ["lunar-stoat"]}
        assert echo == Message(
            id="m-9", sender="operator", content="The discussion has ended.",
            at=dt.datetime(2026, 9, 24, 9, 55, 47, 123456, tzinfo=dt.UTC),
            mentions=("lunar-stoat",), metadata=_METADATA,
        )
        assert echo.interaction == "i-3"
        assert echo.closed_before == ("i-2", "structural")

    async def test_the_messages_come_oldest_first(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        messages = await client.messages("group:advice-1")
        assert [m.id for m in messages] == ["m-1", "m-2"]
        assert messages[0].metadata == {}
        assert messages[0].closed_before is None
        assert fake.requests[0][1] == "/api/v1/channels/group:advice-1/messages?limit=1000"

    async def test_disarming_patches_the_revision_it_read(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        await client.disarm("group:advice-1")
        (_, read, _, _), (method, path, headers, body) = fake.requests
        assert read == "/api/v1/channels/group:advice-1/config"
        assert (method, path) == ("PATCH", "/api/v1/channels/group:advice-1/config")
        assert headers["If-Match"] == "3"
        assert body == {"autonomous": {"enabled": False}}

    async def test_a_member_becomes_an_observer(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        await client.set_respond("group:advice-1", "velvet-pika", "observer")
        [(method, path, _, body)] = fake.requests
        assert (method, path) == ("PATCH", "/api/v1/channels/group:advice-1/members/velvet-pika")
        assert body == {"respond": "observer"}

    async def test_a_refused_request_names_what_was_refused(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        fake.fail = 404
        with pytest.raises(OrchestratorError, match="POST .*/messages: 404 .*no such channel"):
            await client.post("group:advice-1", "operator", "Hello")

    async def test_an_orchestrator_that_is_not_listening_is_not_healthy(self) -> None:
        async with aiohttp.ClientSession() as session:
            assert not await Orchestrator("http://127.0.0.1:9", session).healthy()

    async def test_the_turns_still_in_flight(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        assert await client.activity("group:advice-1") == {"ripple-kite"}
        assert fake.requests[0][:2] == ("GET", "/api/v1/channels/group:advice-1/activity")

    async def test_a_stalled_orchestrator_is_not_healthy_and_fails_a_request(
        self, fake: _Orchestrator,
    ) -> None:
        """Each request has a limit of its own, so a stalled orchestrator
        cannot hold the start past its deadline."""
        fake.stall = 1.0
        async with aiohttp.ClientSession() as session:
            client = Orchestrator(
                str(fake.server.make_url("")), session, timeout=aiohttp.ClientTimeout(total=0.1),
            )
            assert not await client.healthy()
            with pytest.raises(OrchestratorError, match="GET /api/v1/agents"):
                await client.agents()

    async def test_an_answer_that_is_not_json_is_an_orchestrator_error(
        self, fake: _Orchestrator, client: Orchestrator,
    ) -> None:
        fake.garbage = True
        with pytest.raises(OrchestratorError, match="not JSON"):
            await client.messages("group:advice-1")

    async def test_an_orchestrator_that_is_gone_is_an_orchestrator_error(self) -> None:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(OrchestratorError, match="GET /api/v1/agents"):
                await Orchestrator("http://127.0.0.1:9", session).agents()
