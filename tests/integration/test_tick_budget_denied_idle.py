"""RFC 0023 PR 5 — integration: budget-exhausted TICK loop stays idle.

End-to-end loopback test of the autonomous-TICK wallet wiring:

* :meth:`_LLMPersonaAgent.on_tick` → real action loop → real
  ``LLMClient.create_message`` → real ``WalletClient.lease`` →
  in-process budget-enforcing ``WalletService``.

The wallet denies every lease from the start. The persona's TICK is
expected to:

1. Acquire (and be denied) a lease tagged ``CAUSE_AUTONOMOUS_TICK``.
2. Catch :class:`BudgetExceededError` and return ``[DO_NOTHING]``
   *instead of propagating* so :meth:`TickScheduler` records the tick
   as idle via its existing ``all_do_nothing`` branch.
3. Never reach the provider — the provider mock's
   ``create_message`` must not be awaited at all.

This complements the unit-level
``agents/tests/test_action_loop_tick_lease.py`` by pinning the same
behaviour through the *real* :class:`WalletClient` and the *real*
budget-enforcing wallet servicer used by the chat-budget integration
test (`tests/integration/test_chat_budget_exhaustion.py`). The only
mocked seam is the LLM provider; every other hop runs the production
code path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.generated import wallet_pb2_grpc as walletgrpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tools.registry import clear_registry
from agents.wallet_client import WalletClient


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


# ─── Always-deny wallet ───────────────────────────────────────────────────────


class _AlwaysDenyWalletServicer(walletgrpc.WalletServiceServicer):
    """A wallet that denies every ``AcquireLease`` call.

    The integration test wires this as the agent's wallet so the
    *first* TICK already finds the budget exhausted. Mirrors the
    enforcement shape of `_ChatBudgetWalletServicer` in
    ``test_chat_budget_exhaustion.py`` but with the cap pre-tripped.
    """

    def __init__(self) -> None:
        self.acquire_attempts: list[walletpb.LeaseRequest] = []

    async def AcquireLease(  # noqa: N802
        self, request: walletpb.LeaseRequest, context: object,
    ) -> walletpb.LeaseResponse:
        self.acquire_attempts.append(request)
        return walletpb.LeaseResponse(
            denied=walletpb.LeaseDenied(
                scope="per_agent",
                spent_usd=10.0,
                limit_usd=10.0,
                estimated_usd=0.01,
                message="per_agent budget exceeded (spent=$10.00 limit=$10.00)",
            ),
        )

    async def SettleLease(  # noqa: N802
        self, request: walletpb.SettlementRequest, context: object,
    ) -> walletpb.SettlementAck:
        return walletpb.SettlementAck(success=True)

    async def ReleaseLease(  # noqa: N802
        self, request: walletpb.ReleaseRequest, context: object,
    ) -> walletpb.SettlementAck:
        return walletpb.SettlementAck(success=True)


@pytest.fixture
async def deny_wallet() -> AsyncIterator[tuple[WalletClient, _AlwaysDenyWalletServicer]]:
    servicer = _AlwaysDenyWalletServicer()
    server = grpc.aio.server()
    walletgrpc.add_WalletServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield WalletClient.from_channel(channel, backoff_base=0.0), servicer
    finally:
        await channel.close()
        await server.stop(grace=0.5)


# ─── Persona fixture ─────────────────────────────────────────────────────────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "tick-persona",
    "type": "persona",
    "name": "Tick Persona",
    "role": "Integration-test persona for TICK budget exhaustion",
    "model": "claude-sonnet-4-6",
    "temperature": 0.3,
    "max_llm_calls": 3,
    "max_tokens": 128,
    "persona": {
        "background": "Test fixture.",
        "behavior": {"directness": "balanced"},
    },
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:"},
}


def _provider_never_called() -> AsyncMock:
    """A provider whose ``create_message`` *must* not be awaited.

    Returning a ``MagicMock`` from a side_effect that should be
    impossible to hit lets us assert ``await_count == 0`` at the end
    rather than relying on a noisier `assert False` inside the mock.
    """
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=LLMResponse(
        text="(should never reach the provider)",
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=1, output_tokens=1),
    ))
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    return provider


# ─── Test ────────────────────────────────────────────────────────────────────


async def test_budget_exhausted_tick_loop_stays_idle(
    deny_wallet: tuple[WalletClient, _AlwaysDenyWalletServicer],
) -> None:
    wallet, wallet_servicer = deny_wallet

    provider = _provider_never_called()
    llm_client = LLMClient(provider, wallet=wallet)
    agent = create_persona_agent(
        agent_id="tick-persona",
        config=_PERSONA_CONFIG,
        llm_client=llm_client,
    )
    await agent.initialize_memory()

    try:
        # The empty-context TICK short-circuit (RFC 0017 §F) fires when
        # all three conditions hold: no memory admitted, no active goal,
        # no pending turn. Without bypassing it we'd record an idle for
        # the *empty-context* reason, not the budget-denied one. Stub
        # the goal predicate so the loop proceeds to the LLM call and
        # the wallet has a chance to deny.
        agent._has_active_goal_payload = lambda: True  # type: ignore[method-assign]

        # Drive three TICK events back to back. Every one must be
        # denied by the wallet and short-circuit to DO_NOTHING; the
        # provider must never be contacted.
        for _ in range(3):
            actions = await agent.on_event(AgentEvent(event_type=EventType.TICK))
            assert len(actions) == 1
            assert actions[0].action_type == ActionType.DO_NOTHING, (
                "PR 5: a budget-denied TICK must short-circuit to "
                f"DO_NOTHING, got {actions[0].action_type!r}"
            )

        # Three lease attempts, all denied.
        assert len(wallet_servicer.acquire_attempts) == 3
        # Every lease attempt carries the autonomous-TICK cause and the
        # persona's own agent_id.
        for req in wallet_servicer.acquire_attempts:
            assert req.cause == walletpb.CAUSE_AUTONOMOUS_TICK, (
                f"PR 5: TICK leases must carry CAUSE_AUTONOMOUS_TICK "
                f"(got {req.cause!r})"
            )
            assert req.agent_id == "tick-persona"

        # The provider is the entire point of "fail closed before
        # spending": it must not have been contacted even once.
        assert provider.create_message.await_count == 0, (
            "PR 5: a budget-denied TICK must not reach the LLM provider "
            f"(provider was awaited {provider.create_message.await_count}x)"
        )
    finally:
        await agent.close_memory()
