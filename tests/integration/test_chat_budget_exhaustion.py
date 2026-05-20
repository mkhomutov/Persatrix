"""RFC 0023 PR 4 — integration: chat budget exhaustion mid-conversation.

End-to-end loopback test of the chat-path wallet wiring:

* ``stub.SendChatMessage`` → ``EventDispatcher.dispatch`` →
  ``_LLMPersonaAgent.on_event`` → real action loop → real
  ``LLMClient.create_message`` → real ``WalletClient.lease`` →
  in-process budget-enforcing ``WalletService``.

The wallet's USD cap admits exactly one chat turn. The first chat turn
acquires a lease, settles, and the persona's reply lands as
``reply_status="ok"``. The second turn's lease is denied — the provider
is never reached — and the chat handler surfaces the denial as
``reply_status="error"`` with the wallet's ``LeaseDenied.message`` in
``reply``. A third turn (same chat session) is also denied, pinning the
"every subsequent chat is also denied until reset" half of the RFC 0023
PR 4 acceptance.

This complements the agent-side unit tests in
``agents/tests/test_action_loop_chat_lease.py`` and
``agents/tests/test_chat_path_budget_denial.py`` by exercising the
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

from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
from agents.generated import wallet_pb2 as walletpb
from agents.generated import wallet_pb2_grpc as walletgrpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.server_servicers import AgentServiceServicer
from agents.tools.registry import clear_registry
from agents.wallet_client import WalletClient


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


# ─── In-process budget-enforcing WalletService ────────────────────────────────


class _ChatBudgetWalletServicer(walletgrpc.WalletServiceServicer):
    """Wallet that grants leases until a flat USD cap, then denies forever.

    Mirrors ``tests/integration/test_wallet_lease_workflow.py``'s servicer
    but does *not* reverse charges on release — once a chat session
    exhausts the budget, subsequent chats must also be denied (RFC 0023
    PR 4 acceptance: "subsequent chats are also denied until the budget
    resets"). A real wallet reverses on release; a real chat that settled
    successfully does not release.
    """

    def __init__(self, *, max_usd: float, usd_per_call: float) -> None:
        self._max_usd = max_usd
        self._usd_per_call = usd_per_call
        self._spent = 0.0
        self._next_id = 0
        self.acquired = 0
        self.denied = 0
        self.settled = 0

    async def AcquireLease(  # noqa: N802
        self, request: walletpb.LeaseRequest, context: object,
    ) -> walletpb.LeaseResponse:
        self.acquired += 1
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
        # Deliberately *not* reversing the spend — see class docstring.
        return walletpb.SettlementAck(success=True)


@pytest.fixture
async def wallet_one_call() -> AsyncIterator[tuple[WalletClient, _ChatBudgetWalletServicer]]:
    """A WalletClient backed by a wallet that admits exactly one call."""
    servicer = _ChatBudgetWalletServicer(max_usd=0.5, usd_per_call=0.4)
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
    "id": "chat-persona",
    "type": "persona",
    "name": "Chat Persona",
    "role": "Integration-test persona for chat-budget exhaustion",
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


def _persona_reply(text: str = "Hello, human!") -> LLMResponse:
    """Build a persona response that the chat-reply extractor picks up.

    Uses ``COMPLETE_TASK`` rather than ``SEND_CHANNEL_MESSAGE`` because the
    persona action validator rejects ``SEND_CHANNEL_MESSAGE`` without a
    ``channel_id`` (the chat path has none). ``COMPLETE_TASK.result`` is
    priority-3 in ``chat_reply.extract_chat_reply``, which is the path
    chat traffic actually exercises today.
    """
    return LLMResponse(
        text=(
            '```json\n'
            f'[{{"action_type": "complete_task", '
            f'"payload": {{"result": "{text}"}}}}]\n```'
        ),
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=20, output_tokens=12),
    )


# ─── Test ────────────────────────────────────────────────────────────────────


async def test_chat_budget_exhaustion_mid_conversation(
    wallet_one_call: tuple[WalletClient, _ChatBudgetWalletServicer],
) -> None:
    wallet, wallet_servicer = wallet_one_call

    provider = AsyncMock()
    provider.name = "anthropic"
    # The wallet caps the conversation at one provider call. ``side_effect``
    # is a list of length 1: a (wrongly) leased second provider call would
    # raise StopIteration and fail the test loudly.
    provider.create_message = AsyncMock(side_effect=[_persona_reply()])
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])

    llm_client = LLMClient(provider, wallet=wallet)
    agent = create_persona_agent(
        agent_id="chat-persona",
        config=_PERSONA_CONFIG,
        llm_client=llm_client,
    )
    await agent.initialize_memory()

    try:
        dispatcher = EventDispatcher(agents={"chat-persona": agent})
        servicer = AgentServiceServicer({"chat-persona": agent}, dispatcher)

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            # ── Turn 1: budget admits the call ─────────────────
            resp1 = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-persona",
                    user_id="user-1",
                    message="first message",
                ),
            )
            session = resp1.chat_session_id
            assert session
            assert resp1.reply_status == "ok"
            assert resp1.reply == "Hello, human!"

            # ── Turn 2: budget exhausted → denied lease ────────
            resp2 = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-persona",
                    user_id="user-1",
                    message="second message",
                    chat_session_id=session,
                ),
            )
            assert resp2.reply_status == "error"
            assert "budget exceeded" in resp2.reply.lower()
            assert resp2.chat_session_id == session

            # ── Turn 3: still denied (budget did not reset) ────
            resp3 = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-persona",
                    user_id="user-1",
                    message="third message",
                    chat_session_id=session,
                ),
            )
            assert resp3.reply_status == "error"
            assert "budget exceeded" in resp3.reply.lower()

            # The provider was contacted exactly once — the two budget-denied
            # turns never reached it. Three lease acquisitions total: 1 granted,
            # 2 denied. One settle (for the successful first turn).
            assert provider.create_message.await_count == 1
            assert wallet_servicer.acquired == 3
            assert wallet_servicer.denied == 2
            assert wallet_servicer.settled == 1

            await channel.close()
        finally:
            await server.stop(grace=0)
    finally:
        await agent.close_memory()
