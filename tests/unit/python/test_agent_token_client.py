"""The agent's side of agent tokens: what goes on the wire (RFC 0009 Phases 3–4
plan, PR 7).

Registration answers with a token; the agent keeps it, shows it on every call
to the orchestrator, and shows the fleet enrollment secret when it registers.
What a refused token does next is in ``test_agent_token_refusal.py``. A
loopback orchestrator answers, so no call leaves the machine.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from yarl import URL

from agents.orchestrator_session import (
    AGENT_TOKEN_HEADER,
    ENROLLMENT_SECRET_ENV,
    token_middleware,
)
from agents.server import AgentServer
from agents.server_reregister import AgentTokenKeeper
from agents.tools import builtin
from agents.tools.permissions import PermissionGate

from ._agent_token_helpers import (
    AGENT,
    PUBLISH,
    REGISTER,
    FakeOrchestrator,
    StubAgent,
    make_server,
    publish,
    running_orchestrator,
    serve,
    started_server,
)

SECRET = "the-fleet-secret"


@pytest.fixture
async def orchestrator() -> AsyncIterator[FakeOrchestrator]:
    async with running_orchestrator() as fake:
        yield fake


@pytest.fixture
async def server(orchestrator: FakeOrchestrator) -> AsyncIterator[AgentServer]:
    async with started_server(orchestrator) as started:
        yield started


# ─── The secret ──────────────────────────────────────────────────────────────


async def test_registration_shows_the_secret(
    orchestrator: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENROLLMENT_SECRET_ENV, SECRET)
    server = make_server(orchestrator.url)
    await server.start()
    await server.stop()

    assert orchestrator.to(REGISTER)[0].secret == SECRET


async def test_no_secret_set_sends_no_header(
    orchestrator: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENROLLMENT_SECRET_ENV, raising=False)
    server = make_server(orchestrator.url)
    await server.start()
    await server.stop()

    assert orchestrator.to(REGISTER)[0].secret is None


async def test_a_secret_read_from_a_file_loses_its_line_break(
    orchestrator: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiohttp refuses a header holding a line break, which would read as an
    unreachable orchestrator. The orchestrator trims its copy the same way."""
    monkeypatch.setenv(ENROLLMENT_SECRET_ENV, f"  {SECRET}\n")
    server = make_server(orchestrator.url)
    await server.start()
    await server.stop()

    assert orchestrator.to(REGISTER)[0].secret == SECRET


async def test_the_secret_goes_on_registration_only(
    orchestrator: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENROLLMENT_SECRET_ENV, SECRET)
    server = make_server(orchestrator.url)
    await server.start()
    try:
        await publish(server, orchestrator)
    finally:
        await server.stop()

    others = [c for c in orchestrator.calls if c.path != REGISTER]
    assert others
    assert [c.secret for c in others] == [None] * len(others)


async def test_registration_does_not_follow_a_redirect_with_the_secret(
    orchestrator: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator's address answering with a redirect must not hand the
    secret to wherever it points."""
    elsewhere = FakeOrchestrator()
    elsewhere.url, runner = await serve(elsewhere.handle)
    monkeypatch.setenv(ENROLLMENT_SECRET_ENV, SECRET)
    orchestrator.redirect_to = f"{elsewhere.url}{REGISTER}"
    server = make_server(orchestrator.url)
    await server.start()
    try:
        assert await server._self_register() is False
    finally:
        orchestrator.redirect_to = ""
        await server.stop()
        await runner.cleanup()

    assert elsewhere.calls == []


# ─── The token on the wire ───────────────────────────────────────────────────


async def test_calls_after_registration_carry_the_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    await publish(server, orchestrator)

    published = orchestrator.to(PUBLISH)
    assert [c.token for c in published] == [orchestrator.issued[0]]
    assert [c.agent_id for c in published] == [AGENT]


async def test_deregistration_carries_the_token(orchestrator: FakeOrchestrator) -> None:
    """G-6 admits a deregistration on the token of the agent the path names."""
    server = make_server(orchestrator.url)
    await server.start()
    await server.stop()

    deregistration = orchestrator.to(f"/api/v1/agents/{AGENT}", method="DELETE")
    assert [c.token for c in deregistration] == [orchestrator.issued[0]]


async def test_registration_never_carries_a_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """A registration is how a refused token is replaced, so a route that
    refuses bad tokens must never see the stale one on it."""
    orchestrator.restart()
    assert await server._self_register() is True

    assert [c.token for c in orchestrator.to(REGISTER)] == [None, None]


async def test_a_redirect_to_another_port_carries_no_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """G-5: the token goes only on calls to the orchestrator."""
    elsewhere = FakeOrchestrator()
    elsewhere.url, runner = await serve(elsewhere.handle)
    orchestrator.redirect_to = f"{elsewhere.url}/somewhere"
    try:
        await publish(server, orchestrator)
    finally:
        orchestrator.redirect_to = ""
        await runner.cleanup()

    assert [c.token for c in orchestrator.to(PUBLISH)] == [orchestrator.issued[0]]
    assert [c.token for c in elsewhere.calls] == [None]


async def test_a_redirect_to_another_name_on_the_same_port_carries_no_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.redirect_to = f"http://localhost:{URL(orchestrator.url).port}/somewhere"
    try:
        await publish(server, orchestrator)
    finally:
        orchestrator.redirect_to = ""

    assert [c.token for c in orchestrator.to("/somewhere")] == [None]


@pytest.mark.parametrize(
    ("orchestrator_url", "called", "shown"),
    [
        pytest.param("http://orch.example:8080", "http://orch.example:8080/api", True,
                     id="the orchestrator"),
        pytest.param("http://orch.example:8080", "http://evil.example:8080/api", False,
                     id="another host"),
        pytest.param("http://orch.example:8080", "http://orch.example:9090/api", False,
                     id="another port"),
        pytest.param("http://orch.example:8080", "https://orch.example:8080/api", False,
                     id="another scheme"),
        pytest.param("https://orch.example", "http://orch.example/api", False,
                     id="https dropped"),
        pytest.param("https://orch.example", "https://orch.example:443/api", True,
                     id="https's default port spelled out"),
        pytest.param("http://orch.example:80", "http://orch.example/api", True,
                     id="http's default port spelled out"),
        pytest.param("http://ORCH.example:8080", "http://orch.example:8080/api", True,
                     id="a host in capitals"),
        pytest.param("http://[0:0:0:0:0:0:0:1]:8080", "http://[::1]:8080/api", True,
                     id="an IPv6 address spelled out"),
    ],
)
async def test_the_token_goes_to_the_orchestrators_origin_alone(
    orchestrator_url: str, called: str, shown: bool,
) -> None:
    tokens = AgentTokenKeeper(MagicMock())
    tokens.token = "agt1.fake"
    request: Any = SimpleNamespace(url=URL(called), headers={})
    answer = MagicMock(status=200)
    sent: dict[str, str] = {}

    async def handler(sending: Any) -> Any:
        sent.update(sending.headers)
        return answer

    assert await token_middleware(orchestrator_url, tokens)(request, handler) is answer
    assert (AGENT_TOKEN_HEADER in sent) is shown


async def test_a_failed_calls_error_does_not_show_the_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """``repr`` of aiohttp's response error prints the request's headers, and
    the event loop logs a failed wake with ``%r``."""
    orchestrator.publish_answer = b'{"error": "no"}'
    orchestrator.publish_status = 403
    assert server._session is not None

    with pytest.raises(aiohttp.ClientResponseError) as failed:
        async with server._session.post(f"{orchestrator.url}{PUBLISH}", json={}) as resp:
            resp.raise_for_status()

    token = orchestrator.issued[0]
    assert [c.token for c in orchestrator.to(PUBLISH)] == [token]
    assert AGENT in repr(failed.value)  # it does print the headers
    assert token not in repr(failed.value)


async def test_the_wallet_shows_the_current_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    assert server._wallet_client is not None
    assert dict(server._wallet_client._metadata or ())[AGENT_TOKEN_HEADER] == orchestrator.issued[0]

    await server._self_register()

    assert dict(server._wallet_client._metadata or ())[AGENT_TOKEN_HEADER] == orchestrator.issued[1]


async def test_http_request_tool_carries_neither_credential(
    server: AgentServer, orchestrator: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool the model drives opens a session of its own, so neither the
    token nor the secret reaches a host a model chose."""
    monkeypatch.setenv(ENROLLMENT_SECRET_ENV, SECRET)
    outside = FakeOrchestrator()
    outside.url, runner = await serve(outside.handle)
    monkeypatch.setattr(
        builtin, "permission_gate",
        PermissionGate({"network": {"allow": ["127.0.0.1"], "deny": ["*"]}}),
    )
    try:
        result = await builtin.http_request(f"{outside.url}/page")
    finally:
        await runner.cleanup()

    assert result.success, result.error
    assert [(c.token, c.secret, c.agent_id) for c in outside.calls] == [(None, None, None)]


async def test_an_orchestrator_that_answers_with_no_token_still_registers(
    orchestrator: FakeOrchestrator,
) -> None:
    """One older than PR 6, mid-rollout: registered, and nothing to send."""
    orchestrator.answer_with_token = False
    server = make_server(orchestrator.url)
    await server.start()
    try:
        assert await server._self_register() is True
        await publish(server, orchestrator)
    finally:
        await server.stop()

    assert [c.token for c in orchestrator.to(PUBLISH)] == [None]


async def test_a_second_hosted_agent_does_not_replace_the_token(
    orchestrator: FakeOrchestrator,
) -> None:
    """The session names the first hosted agent, so its token is the one to
    send: another agent's beside its ID is what PR 8 refuses."""
    server = make_server(orchestrator.url)
    server.register_agent(StubAgent(agent_id="quiet-fox", config={"capabilities": []}))
    await server.start()
    try:
        await publish(server, orchestrator)
    finally:
        await server.stop()

    assert [c.token for c in orchestrator.to(PUBLISH)] == [orchestrator.issued[0]]


async def test_neither_credential_reaches_a_log_line(
    orchestrator: FakeOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(ENROLLMENT_SECRET_ENV, SECRET)
    caplog.set_level(logging.DEBUG)
    server = make_server(orchestrator.url)
    await server.start()
    try:
        orchestrator.restart()
        await publish(server, orchestrator)
    finally:
        await server.stop()

    assert len(orchestrator.issued) == 2
    for secret in [SECRET, *orchestrator.issued]:
        assert secret not in caplog.text
