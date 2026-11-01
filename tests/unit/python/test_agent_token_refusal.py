"""What a refused agent token does next (RFC 0009 Phases 3–4 plan, PR 7).

When a call is refused for its token the agent registers once for all the
calls in flight and retries each call once, unless the refusal says the token
was revoked, which an operator did on purpose (the plan's F-12 and F-13). A
loopback orchestrator answers, so no call leaves the machine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web

from agents.orchestrator_session import TOKEN_EXPIRED, TOKEN_INVALID, TOKEN_REVOKED
from agents.server import AgentServer
from agents.server_reregister import AgentTokenKeeper

from ._agent_token_helpers import (
    PUBLISH,
    FakeOrchestrator,
    make_server,
    publish,
    running_orchestrator,
    started_server,
)


@pytest.fixture
async def orchestrator() -> AsyncIterator[FakeOrchestrator]:
    async with running_orchestrator() as fake:
        yield fake


@pytest.fixture
async def server(orchestrator: FakeOrchestrator) -> AsyncIterator[AgentServer]:
    async with started_server(orchestrator) as started:
        yield started


# ─── A refused token ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [TOKEN_INVALID, TOKEN_EXPIRED])
async def test_a_refused_token_registers_once_and_the_retried_call_succeeds(
    server: AgentServer, orchestrator: FakeOrchestrator, code: str,
) -> None:
    first = orchestrator.issued[0]
    orchestrator.refuse_current(code)

    assert await publish(server, orchestrator) == 201

    assert orchestrator.registrations == 2
    published = orchestrator.to(PUBLISH)
    assert [c.token for c in published] == [first, orchestrator.issued[1]]
    assert published[0].body == published[1].body != b""


async def test_an_orchestrator_restart_costs_one_round_trip(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.restart()

    assert await publish(server, orchestrator) == 201
    assert await publish(server, orchestrator) == 201

    assert orchestrator.registrations == 2
    assert len(orchestrator.to(PUBLISH)) == 3


async def test_a_second_refusal_is_not_retried(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.refuse_current(TOKEN_INVALID)
    real_register = orchestrator._register

    async def refused_again(request: web.Request) -> web.StreamResponse:
        answer = await real_register(request)
        orchestrator.refuse_current(TOKEN_INVALID)
        return answer

    orchestrator._register = refused_again  # type: ignore[method-assign]

    assert await publish(server, orchestrator) == 401

    assert orchestrator.registrations == 2
    assert len(orchestrator.to(PUBLISH)) == 2


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(b'{"error": "x", "code": "UNAUTHORIZED"}', id="an account refusal"),
        pytest.param(b"not json", id="no JSON"),
        pytest.param(b'["a list"]', id="no object"),
    ],
)
async def test_a_401_that_names_no_token_is_left_alone(
    server: AgentServer, orchestrator: FakeOrchestrator, answer: bytes,
) -> None:
    orchestrator.publish_answer = answer
    assert server._session is not None

    async with server._session.post(f"{orchestrator.url}{PUBLISH}", json={}) as resp:
        assert resp.status == 401
        assert await resp.read() == answer

    assert orchestrator.registrations == 1
    assert len(orchestrator.to(PUBLISH)) == 1


async def test_a_call_that_showed_no_token_is_left_alone(
    orchestrator: FakeOrchestrator,
) -> None:
    """Nothing was shown, so nothing was refused for its token."""
    orchestrator.answer_with_token = False
    orchestrator.publish_answer = b'{"error": "x", "code": "AGENT_TOKEN_INVALID"}'
    async with started_server(orchestrator) as server:
        assert await publish(server, orchestrator) == 401

    assert orchestrator.registrations == 1
    assert len(orchestrator.to(PUBLISH)) == 1


async def test_calls_in_flight_share_one_registration(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.restart()
    orchestrator.registration_gate = asyncio.Event()

    calls = [asyncio.create_task(publish(server, orchestrator)) for _ in range(8)]
    while orchestrator.registrations < 2:
        await asyncio.sleep(0)
    while len(orchestrator.to(PUBLISH)) < 8:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    orchestrator.registration_gate.set()

    assert await asyncio.gather(*calls) == [201] * 8
    assert orchestrator.registrations == 2
    retried = [c.token for c in orchestrator.to(PUBLISH)][8:]
    assert retried == [orchestrator.issued[1]] * 8


async def test_calls_in_flight_share_one_failed_registration(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.restart()
    orchestrator.registration_status = 503
    orchestrator.registration_gate = asyncio.Event()

    calls = [asyncio.create_task(publish(server, orchestrator)) for _ in range(4)]
    while len(orchestrator.to(PUBLISH)) < 4:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    orchestrator.registration_gate.set()

    assert await asyncio.gather(*calls) == [401] * 4
    assert orchestrator.registrations == 2


async def test_a_failed_registration_leaves_the_call_refused_and_the_next_call_tries_again(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.restart()
    orchestrator.registration_status = 401  # a wrong secret, say

    assert await publish(server, orchestrator) == 401
    assert orchestrator.registrations == 2

    orchestrator.registration_status = 201
    assert await publish(server, orchestrator) == 201
    assert orchestrator.registrations == 3


async def test_a_call_refused_as_revoked_retries_with_the_newer_token_held(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """F-12: a registration retires the token a call already on its way shows,
    so the refusal says revoked though no operator did anything."""
    old = orchestrator.issued[0]
    await server._self_register()
    new = orchestrator.issued[1]
    assert orchestrator.refusals[old] == TOKEN_REVOKED

    assert await server._tokens.after_refusal(old, revoked=True) == new
    assert orchestrator.registrations == 2


async def test_a_call_refused_as_revoked_waits_for_the_registration_that_retired_it(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """The orchestrator has issued the new token and the answer is on its way:
    the call must not read that as an operator's revocation."""
    old = orchestrator.issued[0]
    orchestrator.registration_gate = asyncio.Event()
    registering = asyncio.create_task(server._tokens.register())
    while orchestrator.registrations < 2:
        await asyncio.sleep(0)

    refused = asyncio.create_task(server._tokens.after_refusal(old, revoked=True))
    await asyncio.sleep(0)
    orchestrator.registration_gate.set()

    assert await refused == orchestrator.issued[1]
    assert await registering is True
    assert await publish(server, orchestrator) == 201


async def test_a_caller_timing_out_does_not_cut_the_registration_short(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """A cancelled registration may have been issued an answer nobody reads,
    so it belongs to no one caller."""
    orchestrator.restart()
    orchestrator.registration_gate = asyncio.Event()
    assert server._session is not None

    with pytest.raises(TimeoutError):
        async with server._session.post(
            f"{orchestrator.url}{PUBLISH}", json={},
            timeout=aiohttp.ClientTimeout(total=0.05),
        ):
            pass
    orchestrator.registration_gate.set()

    assert await server._tokens.register() is True
    assert await publish(server, orchestrator) == 201
    assert orchestrator.registrations == 2


# ─── A revoked token ─────────────────────────────────────────────────────────


async def test_a_client_told_revoked_does_not_register_again(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """F-13: a revocation is an operator's deregistration, which registering
    again would undo."""
    orchestrator.refuse_current(TOKEN_REVOKED)

    assert await publish(server, orchestrator) == 401

    assert orchestrator.registrations == 1
    assert len(orchestrator.to(PUBLISH)) == 1


async def test_later_refusals_of_a_revoked_token_do_not_register_again(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """F-13: the issuer forgets the agent, and the same token then reads as
    expired, so the client has to remember what it was told."""
    orchestrator.refuse_current(TOKEN_REVOKED)
    await publish(server, orchestrator)

    orchestrator.refuse_current(TOKEN_EXPIRED)
    assert await publish(server, orchestrator) == 401
    orchestrator.refuse_current(TOKEN_INVALID)
    assert await publish(server, orchestrator) == 401

    assert orchestrator.registrations == 1


async def test_a_revocation_is_logged_once(
    server: AgentServer, orchestrator: FakeOrchestrator, caplog: pytest.LogCaptureFixture,
) -> None:
    """It is an operator's business to see, but every later call is refused too."""
    orchestrator.refuse_current(TOKEN_REVOKED)
    caplog.set_level(logging.ERROR, logger="Persatrix.agent.server_reregister")

    await publish(server, orchestrator)
    await publish(server, orchestrator)

    assert caplog.text.count("revoked") == 1


async def test_a_reconnect_does_not_register_a_revoked_agent_again(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """The ISSUE-0125 path stops too: the watcher takes "nothing to do" for a
    landed attempt, so it neither retries nor reports a failure."""
    orchestrator.refuse_current(TOKEN_REVOKED)
    await publish(server, orchestrator)
    assert server._reregister_watcher is not None

    await server._reregister_watcher._fire()

    assert orchestrator.registrations == 1


async def test_a_revocation_is_believed_again_once_a_registration_is_answered(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.refuse_current(TOKEN_INVALID)
    assert await publish(server, orchestrator) == 201
    assert orchestrator.registrations == 2

    orchestrator.refuse_current(TOKEN_REVOKED)
    assert await publish(server, orchestrator) == 401

    assert orchestrator.registrations == 2


async def test_a_refusal_as_a_registration_starts_is_not_read_as_a_revocation() -> None:
    """The registration is under way before its first step has run, and it may
    be what retired the token."""
    answer = MagicMock()
    answer.json = AsyncMock(return_value={"agent_token": "new"})

    async def register() -> bool:
        await keeper.keep_from(answer)
        return True

    keeper = AgentTokenKeeper(register)
    keeper.token = "old"
    registering = asyncio.create_task(keeper.register())
    await asyncio.sleep(0)  # register() has started the task, which has not run

    assert await keeper.after_refusal("old", revoked=True) == "new"
    assert await registering is True


async def test_a_lost_answer_is_not_read_as_a_revocation(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """The orchestrator issued a token whose answer never came back, which
    retired the token the client still holds. The client registers again:
    stopping would mute the agent over one dropped connection."""
    orchestrator.lose_next_answer = True
    assert await server._tokens.register() is False
    assert orchestrator.refusals[orchestrator.issued[0]] == TOKEN_REVOKED

    assert await publish(server, orchestrator) == 201

    assert orchestrator.registrations == 3


# ─── Shutting down ───────────────────────────────────────────────────────────


async def test_a_call_refused_during_shutdown_does_not_register(
    orchestrator: FakeOrchestrator,
) -> None:
    """Calls still running after deregistration are refused. An agent on its
    way out must not come back because of them."""
    server = make_server(orchestrator.url)
    await server.start()
    orchestrator.restart()
    await server._tokens.close()
    try:
        assert await publish(server, orchestrator) == 401
    finally:
        await server.stop()

    assert orchestrator.registrations == 1


async def test_a_revoked_refusal_during_shutdown_logs_nothing(
    server: AgentServer, orchestrator: FakeOrchestrator, caplog: pytest.LogCaptureFixture,
) -> None:
    """The agent's own deregistration revoked it: nothing to tell an operator."""
    await server._tokens.close()
    orchestrator.refuse_current(TOKEN_REVOKED)
    caplog.set_level(logging.ERROR, logger="Persatrix.agent.server_reregister")

    assert await publish(server, orchestrator) == 401

    assert "revoked" not in caplog.text


async def test_stop_closes_the_keeper_before_deregistering(
    orchestrator: FakeOrchestrator,
) -> None:
    server = make_server(orchestrator.url)
    await server.start()
    order: list[str] = []
    real_close = server._tokens.close
    real_deregister = server._self_deregister

    async def close() -> None:
        order.append("close")
        await real_close()

    async def deregister() -> None:
        order.append("deregister")
        await real_deregister()

    server._tokens.close = close  # type: ignore[method-assign]
    server._self_deregister = deregister  # type: ignore[method-assign]

    await server.stop()

    assert order == ["close", "deregister"]


async def test_closing_waits_for_a_registration_under_way(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    """A deregistration sent past a registration still under way could arrive
    first and leave the agent registered after it left."""
    orchestrator.registration_gate = asyncio.Event()
    registering = asyncio.create_task(server._tokens.register())
    while orchestrator.registrations < 2:
        await asyncio.sleep(0)

    closing = asyncio.create_task(server._tokens.close())
    await asyncio.sleep(0.01)
    assert not closing.done()

    orchestrator.registration_gate.set()
    await closing
    assert await registering is True


async def test_a_reconnect_after_closing_does_not_register(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    await server._tokens.close()

    assert await server._tokens.register() is True

    assert orchestrator.registrations == 1


# ─── The ISSUE-0125 path ─────────────────────────────────────────────────────


async def test_a_reconnect_and_a_refused_call_share_one_registration(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.restart()
    orchestrator.registration_gate = asyncio.Event()

    reconnect = asyncio.create_task(server._tokens.register())
    while orchestrator.registrations < 2:
        await asyncio.sleep(0)
    call = asyncio.create_task(publish(server, orchestrator))
    while not orchestrator.to(PUBLISH):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    orchestrator.registration_gate.set()

    assert await call == 201
    assert await reconnect is True
    assert orchestrator.registrations == 2


async def test_the_watcher_replaces_the_token(
    server: AgentServer, orchestrator: FakeOrchestrator,
) -> None:
    orchestrator.restart()
    assert server._reregister_watcher is not None

    await server._reregister_watcher._fire()

    assert orchestrator.registrations == 2
    assert server._tokens.token == orchestrator.issued[1]


async def test_an_answer_that_is_no_object_leaves_no_token() -> None:
    keeper = AgentTokenKeeper(AsyncMock(return_value=True))
    response = MagicMock()
    response.json = AsyncMock(return_value=["not", "an", "object"])

    await keeper.keep_from(response)

    assert keeper.token == ""
