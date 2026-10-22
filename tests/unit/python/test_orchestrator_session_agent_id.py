"""The agents' REST calls carry the hosted agent's ID (ISSUE-0152 step 3).

The orchestrator's RFC 0009 rate limiter gives each agent ID its own budget,
read from the ``X-Agent-ID`` header (``internal/security/middleware.go``,
``AgentIDHeader``). The agent process's shared REST session sent no header,
so every registration, channel read and publish in the fleet shared one
budget with the CLI and every other caller that sends none: the REST half of
ISSUE-0111's residuals. The wallet's gRPC calls already carry the ID
(``test_wallet_client_identity.py``).

A loopback orchestrator records the header on each call it receives, so no
call leaves the machine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web

import agents.server as agents_server
from agents.convene_client import HTTPConveneClient
from agents.orchestrator_session import AGENT_ID_HEADER, new_orchestrator_session
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.server import AgentServer
from agents.tools.recall import HttpRecallClient

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

Seen = list[tuple[str, str | None]]


@pytest.fixture
async def recording_orchestrator() -> AsyncIterator[tuple[str, Seen]]:
    """Answer every route and record (method and path, X-Agent-ID)."""
    seen: Seen = []

    async def record(request: web.Request) -> web.Response:
        seen.append((f"{request.method} {request.path}", request.headers.get(AGENT_ID_HEADER)))
        if request.method == "GET" and request.path == "/api/v1/channels":
            return web.json_response({"channels": []})
        return web.json_response({}, status=201)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", record)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", seen
    finally:
        await runner.cleanup()


async def test_every_rest_call_carries_the_hosted_agent_id(
    recording_orchestrator: tuple[str, Seen],
) -> None:
    url, seen = recording_orchestrator
    server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1, orchestrator_url=url)
    server.register_agent(
        create_persona_agent(
            agent_id="ember-owl",
            config={**_PERSONA_CONFIG, "id": "ember-owl"},
            llm_client=_make_client(),
        )
    )

    await server.start()
    await server.stop()

    calls = [call for call, _ in seen]
    # Registration, the start-up catch-up's channel list, deregistration.
    assert "POST /api/v1/agents/register" in calls
    assert "GET /api/v1/channels" in calls
    assert "DELETE /api/v1/agents/ember-owl" in calls
    assert [agent_id for _, agent_id in seen] == ["ember-owl"] * len(seen)


async def test_the_per_message_paths_carry_the_hosted_agent_id(
    recording_orchestrator: tuple[str, Seen], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish, history, roster, convene and recall: the calls that spend an
    agent's budget message by message (ISSUE-0187). Each must ride the
    shared session rather than one of its own. Publish, history and roster
    are called through the objects start() wired; convene and recall, which
    need a timer or a permission to fire, through the session start() hands
    their wiring."""
    url, seen = recording_orchestrator
    handed: dict[str, aiohttp.ClientSession] = {}

    def spy(name: str, real: Callable[..., None]) -> Callable[..., None]:
        def wrapper(*args: Any) -> None:
            handed[name] = args[1]  # (agents or schedulers, session, url)
            real(*args)
        return wrapper

    monkeypatch.setattr(agents_server, "wire_convene_clients",
                        spy("convene", agents_server.wire_convene_clients))
    monkeypatch.setattr(agents_server, "wire_recall_tools",
                        spy("recall", agents_server.wire_recall_tools))
    server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1, orchestrator_url=url)
    server.register_agent(
        create_persona_agent(
            agent_id="ember-owl",
            config={**_PERSONA_CONFIG, "id": "ember-owl"},
            llm_client=_make_client(),
        )
    )
    await server.start()
    try:
        seen.clear()
        agent = server.agents["ember-owl"]
        assert isinstance(agent, _LLMPersonaAgent)
        publisher = server._dispatcher.executor.channel_publisher
        assert publisher is not None
        assert agent._history_fetcher is not None
        assert agent._roster_fetcher is not None
        await publisher.publish(
            channel_id="group:x", sender_id="ember-owl", content="hi", mentions=[],
        )
        await agent._history_fetcher.fetch("group:x", limit=5)
        await agent._roster_fetcher.fetch_members("group:x")
        await HTTPConveneClient(orchestrator_url=url, session=handed["convene"]).convene("group:x")
        await HttpRecallClient(session=handed["recall"], orchestrator_url=url).recall(
            participant_id="ember-owl", acting_classification="public", query="q",
        )
        calls = [call for call, _ in seen]
        assert seen and [agent_id for _, agent_id in seen] == ["ember-owl"] * len(seen)
    finally:
        await server.stop()

    assert "POST /api/v1/channels/group:x/messages" in calls
    assert "GET /api/v1/channels/group:x/messages" in calls
    assert "GET /api/v1/channels/group:x" in calls
    assert "POST /api/v1/channels/group:x/convene" in calls
    assert "POST /api/v1/personas/ember-owl/recall" in calls


@pytest.mark.parametrize(
    ("agent_id", "want"),
    [
        pytest.param("ember-owl", {AGENT_ID_HEADER: "ember-owl"}, id="hosted agent"),
        pytest.param(None, {}, id="no agent hosted"),
    ],
)
async def test_session_header(agent_id: str | None, want: dict[str, str]) -> None:
    session = new_orchestrator_session(agent_id)
    try:
        assert dict(session.headers) == want
    finally:
        await session.close()


async def test_no_agent_hosted_sends_no_id_on_either_transport(
    recording_orchestrator: tuple[str, Seen],
) -> None:
    """REST and wallet calls name the same identity: with no agent hosted,
    neither sends an ID (the wallet once sent ``unknown``, a real bucket
    that could be quarantined)."""
    url, _ = recording_orchestrator
    server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1, orchestrator_url=url)
    await server.start()
    try:
        assert server._session is not None
        assert AGENT_ID_HEADER not in server._session.headers
        assert server._wallet_client is not None
        assert server._wallet_client._metadata is None
    finally:
        await server.stop()
