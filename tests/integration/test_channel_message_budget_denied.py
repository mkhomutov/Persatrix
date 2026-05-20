"""RFC 0023 PR 6 — integration: channel-message budget-denied lease.

End-to-end loopback test of the channel-message wallet wiring:

* :meth:`_LLMPersonaAgent.on_event` (with a ``CHANNEL_MESSAGE`` event
  built like ``ReceiveChannelMessage`` builds it — no
  ``chat_session_id`` metadata) → real action loop →
  RFC 0011 response gate → real ``LLMClient.create_message`` → real
  ``WalletClient.lease`` → in-process budget-enforcing
  ``WalletService``.

Three behaviours are pinned:

1. **Positive gate → lease acquired tagged ``CAUSE_CHANNEL_MESSAGE``,
   provider reached.** The gate admits, the wallet grants, the provider
   replies, and the wallet records exactly one
   ``CAUSE_CHANNEL_MESSAGE`` lease attempt.
2. **Budget-denied → ``BudgetExceededError`` raised, provider never
   reached.** Once the wallet is over budget, a second channel reply
   gets a denial. The provider mock asserts await_count == 1.
3. **Gated-out event → no lease, no provider.** A ``when_mentioned``
   policy with no mention is suppressed by the gate *before* any wallet
   work; the wallet servicer records zero acquire attempts for that
   event.

This complements the agent-side unit tests in
``agents/tests/test_action_loop_channel_lease.py`` by exercising the
*loopback* shape — only the Go ``WalletService`` is mocked, every other
hop runs the production code path.
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
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry
from agents.wallet_client import BudgetExceededError, WalletClient


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


# ─── In-process budget-enforcing WalletService ────────────────────────────────


class _ChannelBudgetWalletServicer(walletgrpc.WalletServiceServicer):
    """Wallet that grants leases until a flat USD cap, then denies forever.

    Mirrors the chat-budget-exhaustion servicer shape:
    :class:`tests.integration.test_chat_budget_exhaustion._ChatBudgetWalletServicer`.
    A real wallet reverses charges on release; this one does not, so a
    second channel-message reply after the budget is exhausted is also
    denied. ``acquired_causes`` records the cause of every attempt so the
    test can assert PR 6's tagging at the wire boundary.
    """

    def __init__(self, *, max_usd: float, usd_per_call: float) -> None:
        self._max_usd = max_usd
        self._usd_per_call = usd_per_call
        self._spent = 0.0
        self._next_id = 0
        self.acquired = 0
        self.denied = 0
        self.settled = 0
        self.acquired_causes: list[walletpb.Cause.ValueType] = []

    async def AcquireLease(  # noqa: N802
        self, request: walletpb.LeaseRequest, context: object,
    ) -> walletpb.LeaseResponse:
        self.acquired += 1
        self.acquired_causes.append(request.cause)
        if self._spent + self._usd_per_call > self._max_usd:
            self.denied += 1
            return walletpb.LeaseResponse(
                denied=walletpb.LeaseDenied(
                    scope="per_agent",
                    spent_usd=self._spent,
                    limit_usd=self._max_usd,
                    estimated_usd=self._usd_per_call,
                    message=(
                        f"per_agent budget exceeded "
                        f"(spent=${self._spent:.2f} limit=${self._max_usd:.2f})"
                    ),
                ),
            )
        self._spent += self._usd_per_call
        self._next_id += 1
        return walletpb.LeaseResponse(
            grant=walletpb.LeaseGrant(
                lease_id=f"lease-{self._next_id}",
                granted_input_tokens=request.estimated_input_tokens,
                granted_output_tokens=request.estimated_max_output_tokens,
                ttl_seconds=60,
            ),
        )

    async def SettleLease(  # noqa: N802
        self, request: walletpb.SettlementRequest, context: object,
    ) -> walletpb.SettlementAck:
        self.settled += 1
        return walletpb.SettlementAck(success=True)

    async def ReleaseLease(  # noqa: N802
        self, request: walletpb.ReleaseRequest, context: object,
    ) -> walletpb.SettlementAck:
        return walletpb.SettlementAck(success=True)


@pytest.fixture
async def wallet_one_call() -> AsyncIterator[
    tuple[WalletClient, _ChannelBudgetWalletServicer]
]:
    """A WalletClient backed by a wallet that admits exactly one call."""
    servicer = _ChannelBudgetWalletServicer(max_usd=0.5, usd_per_call=0.4)
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
    "id": "channel-persona",
    "type": "persona",
    "name": "Channel Persona",
    "role": "Integration-test persona for channel-message budget wiring",
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


def _persona_reply(text: str = "Hello, channel!") -> LLMResponse:
    return LLMResponse(
        text=(
            '```json\n'
            f'[{{"action_type": "complete_task", '
            f'"payload": {{"result": "{text}"}}}}]\n```'
        ),
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=20, output_tokens=12),
    )


def _channel_event(
    *,
    channel_id: str = "chan-abc",
    sender_id: str = "peer-1",
    respond_policy: str = "always",
    mentions: list[str] | None = None,
) -> AgentEvent:
    """A ``CHANNEL_MESSAGE`` event shaped like ``ReceiveChannelMessage`` builds.

    No ``chat_session_id`` metadata — that is the chat-as-DM
    discriminator from PR 4, and would route to ``CAUSE_CHAT`` instead.
    """
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "hi from channel",
            "channel_type": "general",
            "mentions": mentions or [],
            "respond_policy": respond_policy,
            "thread_parent_sender_id": "",
        },
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-1",
    )


# ─── Test ────────────────────────────────────────────────────────────────────


async def test_channel_message_lease_then_budget_denied(
    wallet_one_call: tuple[WalletClient, _ChannelBudgetWalletServicer],
) -> None:
    """PR 6 acceptance: gate→lease→reply on turn 1, gate→lease-denied on turn 2."""
    wallet, wallet_servicer = wallet_one_call

    provider = AsyncMock()
    provider.name = "anthropic"
    # Budget admits one call; a second leased provider call would
    # raise StopIteration and fail the test loudly.
    provider.create_message = AsyncMock(side_effect=[_persona_reply()])
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])

    llm_client = LLMClient(provider, wallet=wallet)
    agent = create_persona_agent(
        agent_id="channel-persona",
        config=_PERSONA_CONFIG,
        llm_client=llm_client,
    )
    await agent.initialize_memory()

    try:
        # ── Turn 1: budget admits the call ─────────────────
        actions1 = await agent.on_event(_channel_event())
        # The persona's reply is folded into an action list; the
        # important assertion is that the wallet recorded the lease,
        # not the action shape (channel_reply synthesis is tested
        # elsewhere).
        assert actions1, "PR 6: positive gate → action list must be non-empty"

        # ── Turn 2: budget exhausted → denied lease ────────
        with pytest.raises(BudgetExceededError) as excinfo:
            await agent.on_event(_channel_event())
        assert excinfo.value.scope == "per_agent"
        assert "budget exceeded" in str(excinfo.value).lower()

        # Two lease attempts, both tagged CAUSE_CHANNEL_MESSAGE: one
        # granted (turn 1), one denied (turn 2). The provider is
        # reached exactly once — the denied turn never gets there.
        assert wallet_servicer.acquired == 2
        assert wallet_servicer.denied == 1
        assert wallet_servicer.settled == 1
        for cause in wallet_servicer.acquired_causes:
            assert cause == walletpb.CAUSE_CHANNEL_MESSAGE, (
                "PR 6: every wallet lease for a receiver-side channel "
                f"event must carry CAUSE_CHANNEL_MESSAGE (got {cause!r})"
            )
        assert provider.create_message.await_count == 1, (
            "PR 6: a budget-denied channel-message must not reach the LLM "
            f"provider (got {provider.create_message.await_count} calls)"
        )
    finally:
        await agent.close_memory()


async def test_gated_out_channel_event_skips_wallet(
    wallet_one_call: tuple[WalletClient, _ChannelBudgetWalletServicer],
) -> None:
    """PR 6 invariant: the RFC 0011 response gate runs before the wallet.

    A ``when_mentioned`` policy with no mention is suppressed by the
    gate; the wallet servicer must never see a lease for that event.
    """
    wallet, wallet_servicer = wallet_one_call

    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=_persona_reply())
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])

    llm_client = LLMClient(provider, wallet=wallet)
    agent = create_persona_agent(
        agent_id="channel-persona",
        config=_PERSONA_CONFIG,
        llm_client=llm_client,
    )
    await agent.initialize_memory()

    try:
        await agent.on_event(
            _channel_event(respond_policy="when_mentioned", mentions=[]),
        )
        assert wallet_servicer.acquired == 0, (
            "PR 6: the gate must run before lease acquisition — a "
            "suppressed event must not contact the wallet "
            f"(got {wallet_servicer.acquired} acquire attempts)"
        )
        assert provider.create_message.await_count == 0
    finally:
        await agent.close_memory()
