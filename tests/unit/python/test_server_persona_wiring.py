"""
Tests for server persona wiring — _resolve_agent_type, AgentServer memory
lifecycle, and initialize_persona_agents (tick scheduler registration,
partial-failure isolation).

All tests use mock LLM client — no real API calls.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.dispatch import EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.server import AgentServer
from agents.server_persona import _resolve_agent_type
from agents.tools.registry import clear_registry

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    """Create a mock LLMClient that returns the given responses."""
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(text="I'll handle this task.")
        )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ]
    )
    return LLMClient(mock_provider)


_PERSONA_CONFIG: dict = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "role": "Engineering leadership",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "VP of Engineering",
        "background": "15 years in software engineering.",
        "behavior": {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        },
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}

_PERSONA_CONFIG_2: dict = {
    "id": "iron-fox",
    "type": "persona",
    "name": "Iron Fox",
    "role": "Senior developer",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "Senior Engineer",
        "background": "Full-stack developer.",
        "behavior": {},
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}


async def _make_agent(
    config: dict | None = None,
    llm_client: LLMClient | None = None,
) -> _LLMPersonaAgent:
    """Helper to create an initialized _LLMPersonaAgent."""
    cfg = config or {**_PERSONA_CONFIG}
    client = llm_client or _make_client()
    agent = create_persona_agent(
        agent_id=cfg["id"], config=cfg, llm_client=client,
    )
    await agent.initialize_memory()
    return agent


# ─── Server Persona Wiring Tests ────────────────────────────


class TestResolveAgentType:

    def test_task_type(self):
        assert _resolve_agent_type({"id": "test", "type": "task"}) == "task"

    def test_persona_type(self):
        assert _resolve_agent_type({"id": "test", "type": "persona"}) == "persona"

    def test_default_type(self):
        assert _resolve_agent_type({"id": "test"}) == "task"

    def test_unknown_type(self):
        with pytest.raises(SystemExit, match="Unknown agent type"):
            _resolve_agent_type({"id": "test", "type": "alien"})


class TestAgentServerPersonaLifecycle:
    """Test memory lifecycle and tick scheduler wiring in AgentServer."""

    async def test_persona_agent_memory_initialized_on_start(self):
        """Persona agents have memory initialized during server.start()."""
        agent = await _make_agent()
        # close memory so start() can re-initialize
        await agent.close_memory()

        server = AgentServer(port=0)
        server.agents["ember-owl"] = agent

        # Mock gRPC server and network calls
        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            # Spy on initialize_memory
            agent.initialize_memory = AsyncMock()  # type: ignore[method-assign]
            await server.start()
            agent.initialize_memory.assert_awaited_once()

        await server.stop()

    async def test_persona_agent_memory_closed_on_stop(self):
        """Persona agents have memory closed during server.stop()."""
        agent = await _make_agent()
        server = AgentServer(port=0)
        server.agents["ember-owl"] = agent

        agent.close_memory = AsyncMock()  # type: ignore[method-assign]
        await server.stop()
        agent.close_memory.assert_awaited_once()

    async def test_tick_scheduler_started_for_autonomous_agent(self):
        """Agents with autonomy.level=semi-autonomous get a tick scheduler."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
                "max_actions_per_tick": 3,
                "idle_after_ticks": 10,
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer(port=0)
        server.agents["ember-owl"] = agent

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        assert "ember-owl" in server._tick_schedulers
        assert server._tick_schedulers["ember-owl"].is_running
        await server.stop()

    async def test_tick_scheduler_not_started_for_reactive_agent(self):
        """Agents with autonomy.level=reactive (default) do NOT get a tick scheduler."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {"level": "reactive"},
        }
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer(port=0)
        server.agents["ember-owl"] = agent

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        assert "ember-owl" not in server._tick_schedulers
        await server.stop()

    async def test_tick_scheduler_stopped_on_server_stop(self):
        """Tick schedulers are stopped during server shutdown."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "autonomous",
                "tick_interval_seconds": 999,
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer(port=0)
        server.agents["ember-owl"] = agent

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        assert server._tick_schedulers["ember-owl"].is_running
        await server.stop()
        assert len(server._tick_schedulers) == 0

    async def test_memory_init_failure_skips_dispatch_registration(self):
        """If initialize_memory() fails, agent is NOT registered with dispatcher or scheduler.

        An agent whose memory initialization fails would crash on the first
        dispatched event (store_episode() on unopened DB).  The server must
        skip dispatcher and tick scheduler registration for such agents.
        (PR #55 review: memory init failure should prevent dispatch registration.)
        """
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config,
            llm_client=_make_client(),
        )

        server = AgentServer(port=0)
        server.agents["ember-owl"] = agent

        # Force initialize_memory() to fail
        agent.initialize_memory = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("DB connection failed"),
        )

        with patch.object(server, '_self_register', new_callable=AsyncMock):
            mock_grpc = AsyncMock()
            mock_grpc.add_insecure_port = MagicMock(return_value=50051)
            server._server = mock_grpc
            await server.start()

        # Agent should NOT be registered with dispatcher
        assert "ember-owl" not in server._dispatcher._agents
        # Agent should NOT have a tick scheduler
        assert "ember-owl" not in server._tick_schedulers

        await server.stop()


# ─── Direct tests for initialize_persona_agents ─────────────
#
# TestAgentServerPersonaLifecycle above tests the same logic indirectly via
# AgentServer.start(), which requires gRPC server mocking.  These tests call
# initialize_persona_agents() directly to cover scenarios that are awkward to
# exercise through the server: mixed agent dicts, partial init failures, and
# the in-place mutation contract on tick_schedulers.


class TestWireWalletClient:
    """RFC 0023 PR 3 — wire_wallet_client attaches the wallet post-construction."""

    async def test_wallet_attached_to_every_hosted_agent(self):
        from agents.server_persona import wire_wallet_client
        from agents.wallet_client import WalletClient

        client_a = _make_client()
        client_b = _make_client()
        agent_a = await _make_agent(config={**_PERSONA_CONFIG}, llm_client=client_a)
        agent_b = await _make_agent(config={**_PERSONA_CONFIG_2}, llm_client=client_b)
        try:
            wallet = WalletClient(AsyncMock())
            wire_wallet_client(
                {"ember-owl": agent_a, "iron-fox": agent_b}, wallet,
            )
            # Both LLMClients now route through the wallet.
            assert client_a._wallet is wallet
            assert client_b._wallet is wallet
        finally:
            await agent_a.close_memory()
            await agent_b.close_memory()


class TestInitializePersonaAgents:

    async def test_task_agent_is_skipped(self):
        """Non-_LLMPersonaAgent entries in the dict are silently ignored."""
        from agents.server_persona import initialize_persona_agents
        from agents.task_agent import TaskAgent

        task_agent = MagicMock(spec=TaskAgent)
        agents = {"worker": task_agent}
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents(agents, dispatcher, schedulers)

        assert "worker" not in schedulers
        # EventDispatcher has no public "has_agent" API; _agents is the only
        # way to verify registration.
        assert "worker" not in dispatcher._agents

    async def test_persona_agent_registered_with_dispatcher(self):
        """A persona agent with successful memory init is registered with the dispatcher."""
        from agents.server_persona import initialize_persona_agents

        agent = await _make_agent()
        await agent.close_memory()  # let initialize_persona_agents open it

        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents({"ember-owl": agent}, dispatcher, schedulers)

        try:
            assert "ember-owl" in dispatcher._agents
        finally:
            await agent.close_memory()

    async def test_memory_failure_skips_agent_but_others_continue(self):
        """A memory init failure on one agent does not prevent others from initializing.

        The indirect AgentServer tests only exercise single-agent failure.
        This test verifies the multi-agent case: the failing agent is skipped
        and the next agent in the dict is still initialized correctly.
        """
        from agents.server_persona import initialize_persona_agents

        failing_agent = create_persona_agent(
            agent_id="ember-owl",
            config={**_PERSONA_CONFIG},
            llm_client=_make_client(),
        )
        failing_agent.initialize_memory = AsyncMock(
            side_effect=RuntimeError("DB error"),
        )

        ok_agent = await _make_agent(config={**_PERSONA_CONFIG_2})
        await ok_agent.close_memory()

        agents = {"ember-owl": failing_agent, "iron-fox": ok_agent}
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        await initialize_persona_agents(agents, dispatcher, schedulers)

        assert "ember-owl" not in dispatcher._agents
        assert "ember-owl" not in schedulers
        assert "iron-fox" in dispatcher._agents
        await ok_agent.close_memory()

    async def test_tick_schedulers_dict_mutated_in_place(self, caplog):
        """The tick_schedulers dict passed in is mutated: autonomous agents are inserted.

        Validates the in-place mutation contract documented in the function's
        docstring — callers rely on the dict being populated, not on a return value.

        Also asserts that the three COST: warning lines are actually emitted on the
        autonomous path so that a silent regression (accidental deletion or wrong
        condition guard) is caught by the test suite.
        (PR #150 review: cost-warning log lines lacked a dedicated regression test.)
        """
        from agents.server_persona import initialize_persona_agents

        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 999,
                "max_actions_per_tick": 3,
                "idle_after_ticks": 10,
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )

        dispatcher = EventDispatcher()
        schedulers: dict = {}

        with caplog.at_level(logging.WARNING, logger="Persatrix.agent.server_persona"):
            await initialize_persona_agents({"ember-owl": agent}, dispatcher, schedulers)

        assert "ember-owl" in schedulers
        assert schedulers["ember-owl"].is_running
        assert any("COST:" in r.message for r in caplog.records)

        await schedulers["ember-owl"].stop()
        await agent.close_memory()
