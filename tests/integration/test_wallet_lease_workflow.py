"""RFC 0023 PR 3 — integration: an over-budget workflow task fails on the
*second* LLM call.

Wires the real agent-side lease path end to end over a loopback gRPC
connection: ``TaskAgent.handle`` → ``BaseAgent._run_llm_loop`` →
``LLMClient.create_message`` → ``WalletClient.lease`` → a real
``WalletService`` gRPC stub. Only the wallet *servicer* is an in-process
Python stand-in — it grants leases until a USD cap, then denies — because
the Go ``WalletService``'s enforcement is unit-tested separately (PR 2);
this test pins the agent-side contract:

* the first LLM call in a task acquires a lease and settles it;
* once the budget is exhausted, the second call's lease is *denied*;
* the denied call never reaches the provider, and the task fails with a
  *structured* budget error rather than a generic provider error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.base import TaskInput, TaskStatus
from agents.generated import wallet_pb2 as walletpb
from agents.generated import wallet_pb2_grpc as walletgrpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, ToolCall, Usage
from agents.task_agent import TaskAgent
from agents.wallet_client import WalletClient

# ─── In-process budget-enforcing WalletService ────────────────────────────────


class _BudgetWalletServicer(walletgrpc.WalletServiceServicer):
    """A WalletService that grants leases until a flat USD cap, then denies.

    A faithful-enough stand-in for the Go ``WalletService`` for the
    agent-side path: ``AcquireLease`` records a provisional charge and
    denies once the cap would be exceeded; ``ReleaseLease`` reverses it.
    """

    def __init__(self, *, max_usd: float, usd_per_call: float) -> None:
        self._max_usd = max_usd
        self._usd_per_call = usd_per_call
        self._spent = 0.0
        self._next_id = 0
        self.acquired = 0
        self.settled = 0
        self.released = 0

    async def AcquireLease(  # noqa: N802 — gRPC servicer naming.
        self, request: walletpb.LeaseRequest, context: object,
    ) -> walletpb.LeaseResponse:
        self.acquired += 1
        if self._spent + self._usd_per_call > self._max_usd:
            return walletpb.LeaseResponse(
                denied=walletpb.LeaseDenied(
                    scope="global",
                    spent_usd=self._spent,
                    limit_usd=self._max_usd,
                    estimated_usd=self._usd_per_call,
                    message="global budget exceeded",
                ),
            )
        self._spent += self._usd_per_call  # provisional charge
        self._next_id += 1
        return walletpb.LeaseResponse(
            grant=walletpb.LeaseGrant(
                lease_id=f"lease-{self._next_id}",
                granted_input_tokens=request.estimated_input_tokens,
                granted_output_tokens=request.estimated_max_output_tokens,
                ttl_seconds=60,
            ),
        )

    async def SettleLease(  # noqa: N802 — gRPC servicer naming.
        self, request: walletpb.SettlementRequest, context: object,
    ) -> walletpb.SettlementAck:
        self.settled += 1
        return walletpb.SettlementAck(success=True)

    async def ReleaseLease(  # noqa: N802 — gRPC servicer naming.
        self, request: walletpb.ReleaseRequest, context: object,
    ) -> walletpb.SettlementAck:
        self.released += 1
        self._spent -= self._usd_per_call  # reverse the provisional charge
        return walletpb.SettlementAck(success=True)


@pytest.fixture
async def wallet_over_grpc() -> AsyncIterator[tuple[WalletClient, _BudgetWalletServicer]]:
    """A WalletClient connected to an in-process budget-enforcing wallet.

    The cap admits exactly one LLM call: the first lease is granted, the
    second is denied.
    """
    servicer = _BudgetWalletServicer(max_usd=1.0, usd_per_call=0.75)
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


# ─── Test ─────────────────────────────────────────────────────────────────────


async def test_over_budget_workflow_task_fails_on_the_second_llm_call(
    wallet_over_grpc: tuple[WalletClient, _BudgetWalletServicer],
) -> None:
    wallet, servicer = wallet_over_grpc

    # The first response is a tool call so _run_llm_loop iterates: a second
    # create_message is issued, which the now-exhausted budget denies.
    first = LLMResponse(
        text="",
        stop_reason=StopReason.TOOL_USE,
        tool_calls=[ToolCall(id="tc1", name="nonexistent_tool", input={})],
        usage=Usage(input_tokens=30, output_tokens=15),
    )
    provider = AsyncMock()
    provider.name = "anthropic"
    # side_effect with a single entry: a (wrongly) leased second provider
    # call would raise StopIteration and fail the test loudly.
    provider.create_message = AsyncMock(side_effect=[first])
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(
        return_value=[{"role": "user", "content": "tool results"}],
    )

    agent = TaskAgent(
        agent_id="ember-owl",
        config={"model": "claude-sonnet-4-6", "max_llm_calls": 5, "max_tokens": 512},
        llm_client=LLMClient(provider, wallet=wallet),
    )

    output = await agent.handle(
        TaskInput(task_id="t-1", workflow_id="wf-overbudget", payload="do the work"),
    )

    # The task fails with a *structured* budget error, not a provider error.
    assert output.status == TaskStatus.FAILED
    assert "budget exceeded" in output.result
    assert output.metadata["error_type"] == "budget_exceeded"
    assert output.metadata["budget_scope"] == "global"

    # Two lease acquisitions were attempted; the first settled, the second
    # was denied before the provider was ever contacted.
    assert servicer.acquired == 2
    assert servicer.settled == 1
    assert provider.create_message.await_count == 1
