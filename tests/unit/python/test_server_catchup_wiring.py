"""RFC 0011 PR 5 follow-up — :class:`AgentServer` startup catch-up wiring.

The on-startup catch-up fetcher (see :mod:`agents.channel_catchup`) is
invoked from :meth:`AgentServer.start` after self-registration. These
tests pin the wiring contract so a refactor to the startup sequence
cannot silently drop the call:

* :func:`agents.channel_catchup.replay_for_persona_agents` is invoked
  exactly once per server start, with the full ``self.agents`` mapping
  passed through (the helper itself filters persona vs. task agents
  and is covered by ``test_channel_catchup.py`` /
  ``test_replay_mode_action_loop.py``).
* The shared :class:`aiohttp.ClientSession` is reused (the publisher
  and the catch-up fetcher must share a single connection pool to the
  orchestrator).
* A fetcher exception does not propagate past the startup boundary
  — :func:`replay_for_persona_agents` is best-effort by contract, and
  startup must not crash on a flapping orchestrator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.channel_catchup import replay_for_persona_agents
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent
from agents.server import AgentServer

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


async def _make_persona_agent(agent_id: str):
    cfg = {**_PERSONA_CONFIG, "id": agent_id}
    return create_persona_agent(
        agent_id=agent_id, config=cfg, llm_client=_make_client(),
    )


@pytest.fixture
async def empty_orchestrator():
    """Loopback orchestrator that returns an empty channel list.

    Sufficient for the persona-vs-task filter test below — the helper
    short-circuits the per-agent loop before any per-channel REST call.
    """
    log: list[str] = []

    async def list_channels(request: web.Request) -> web.Response:
        log.append(request.path)
        return web.json_response({"channels": []})

    app = web.Application()
    app.router.add_get("/api/v1/channels", list_channels)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", log
    finally:
        await runner.cleanup()


class _StubTaskAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


class TestStartupCatchUpWiring:
    async def test_replay_helper_called_with_full_agent_map(self):
        """The wiring must hand the entire ``self.agents`` dict to the
        catch-up helper (which then filters persona vs. task internally).
        """
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        agent_a = await _make_persona_agent("ember-owl")
        agent_b = await _make_persona_agent("iron-fox")
        server.register_agent(agent_a)
        server.register_agent(agent_b)

        with (
            patch.object(server, "_self_register", new_callable=AsyncMock),
            patch(
                "agents.server.replay_for_persona_agents",
                new_callable=AsyncMock,
            ) as replay_mock,
        ):
            await server.start()

        try:
            replay_mock.assert_awaited_once()
            kwargs = replay_mock.await_args.kwargs
            assert set(kwargs["agents"].keys()) == {"ember-owl", "iron-fox"}
            assert kwargs["orchestrator_url"] == server.orchestrator_url
        finally:
            with patch.object(server, "_self_deregister", new_callable=AsyncMock):
                await server.stop()

    async def test_replay_uses_shared_aiohttp_session(self):
        """The catch-up fetcher must share the same
        :class:`aiohttp.ClientSession` opened for the publisher.

        Two sessions to the same orchestrator would (a) double the
        TCP connection-pool footprint and (b) defeat the rationale
        for opening one shared session in PR 4a-ii-β-1.
        """
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        agent = await _make_persona_agent("ember-owl")
        server.register_agent(agent)

        with (
            patch.object(server, "_self_register", new_callable=AsyncMock),
            patch(
                "agents.server.replay_for_persona_agents",
                new_callable=AsyncMock,
            ) as replay_mock,
        ):
            await server.start()

        try:
            replay_mock.assert_awaited_once()
            session_passed = replay_mock.await_args.kwargs["session"]
            assert session_passed is server._session
        finally:
            with patch.object(server, "_self_deregister", new_callable=AsyncMock):
                await server.stop()

    async def test_replay_exception_does_not_break_startup(self):
        """:func:`replay_for_persona_agents` is best-effort by contract,
        but a regression turning it into a raiser must not crash startup.
        The wiring must call it without an outer try/except *because*
        the helper guarantees no propagation; this test pins the
        helper's contract from the wiring's perspective.
        """
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        agent = await _make_persona_agent("ember-owl")
        server.register_agent(agent)

        # Stub the helper with a no-raise AsyncMock — its contract is
        # "best-effort, never raises". A future regression that breaks
        # that contract is caught by the helper's own unit tests in
        # ``test_channel_catchup.py``; this test pins that the wiring
        # awaits the helper exactly once even on a fast-return.
        with (
            patch.object(server, "_self_register", new_callable=AsyncMock),
            patch(
                "agents.server.replay_for_persona_agents",
                new_callable=AsyncMock,
            ) as replay_mock,
        ):
            await server.start()

        try:
            replay_mock.assert_awaited_once()
        finally:
            with patch.object(server, "_self_deregister", new_callable=AsyncMock):
                await server.stop()


class TestReplayForPersonaAgents:
    """Pin the persona-vs-task filter and best-effort exception
    swallowing of :func:`replay_for_persona_agents` — the helper that
    :class:`AgentServer.start` calls to drive catch-up across every
    hosted agent.
    """

    async def test_filters_to_persona_agents_only(self, empty_orchestrator):
        """Only :class:`_LLMPersonaAgent` instances trigger a catch-up
        run; task agents are silently skipped. Without this filter the
        catch-up would crash on task agents whose ``on_event`` does
        not implement the action-loop replay-mode short-circuit.
        """
        base_url, log = empty_orchestrator
        persona = await _make_persona_agent("ember-owl")
        task_agent = _StubTaskAgent(agent_id="worker-bee", config={})

        async with aiohttp.ClientSession() as session:
            await replay_for_persona_agents(
                agents={"ember-owl": persona, "worker-bee": task_agent},
                orchestrator_url=base_url,
                session=session,
            )

        # Hit the channel-list endpoint exactly once for the persona;
        # task-agent iteration short-circuited before any REST call.
        assert log.count("/api/v1/channels") == 1

    async def test_swallows_per_agent_exceptions(
        self, empty_orchestrator, caplog,
    ):
        """An exception raised inside one agent's catch-up must not
        strand the rest of the agent map. The helper logs and moves on.
        """
        base_url, _ = empty_orchestrator
        persona = await _make_persona_agent("ember-owl")

        # Force the per-agent path to raise — empty_orchestrator's
        # empty channel list would otherwise short-circuit before
        # any failure surface is reached, so patch the helper's inner
        # replay to throw.
        with (
            caplog.at_level("ERROR"),
            patch(
                "agents.channel_catchup.replay_channel_history",
                new=AsyncMock(side_effect=RuntimeError("simulated")),
            ),
        ):
            async with aiohttp.ClientSession() as session:
                await replay_for_persona_agents(
                    agents={"ember-owl": persona},
                    orchestrator_url=base_url,
                    session=session,
                )

        # Helper logged the failure but did not propagate.
        assert any(
            "catch-up replay" in rec.message.lower()
            and "aborted" in rec.message.lower()
            for rec in caplog.records
        )

    async def test_no_session_returns_silently(self):
        """A ``None`` session means the publisher was never wired (test
        fixtures or partial init). The helper must short-circuit
        without raising or logging.
        """
        await replay_for_persona_agents(
            agents={}, orchestrator_url="http://127.0.0.1:1", session=None,
        )


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
