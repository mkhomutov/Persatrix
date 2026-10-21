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

from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from agents.orchestrator_session import AGENT_ID_HEADER, new_orchestrator_session
from agents.persona import create_persona_agent
from agents.server import AgentServer

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
