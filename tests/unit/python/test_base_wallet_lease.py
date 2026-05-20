"""RFC 0023 PR 3 — workflow-task LLM calls acquire a wallet lease.

``BaseAgent._run_llm_loop`` is the workflow-task LLM-call origin. When the
agent's :class:`LLMClient` carries a wallet, every call here acquires a
``CAUSE_WORKFLOW_TASK`` lease before issuing; a budget denial surfaces as
a *structured* ``TaskStatus.FAILED`` rather than a generic provider error.

The wallet runs through the real :class:`WalletClient` over a mocked gRPC
stub; the LLM provider is mocked at the boundary — no real network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.wallet_client import WalletClient

# ─── Helpers ──────────────────────────────────────────────────────────────────


class _TestableAgent(BaseAgent):
    """Minimal agent that delegates straight to ``_run_llm_loop``."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return await self._run_llm_loop(task, system_prompt="You are a test agent.")


def _wallet_stub(*, acquire: object = None) -> AsyncMock:
    stub = AsyncMock()
    stub.AcquireLease = AsyncMock(
        return_value=acquire or walletpb.LeaseResponse(
            grant=walletpb.LeaseGrant(
                lease_id="01J000000000000000000WF",
                granted_input_tokens=500,
                granted_output_tokens=500,
                ttl_seconds=60,
            ),
        ),
    )
    stub.SettleLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    stub.ReleaseLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    return stub


def _agent(*, wallet: WalletClient | None = None) -> _TestableAgent:
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text="done",
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=12, output_tokens=34),
        ),
    )
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    client = LLMClient(provider, wallet=wallet)
    return _TestableAgent(
        agent_id="ember-owl",
        config={"model": "claude-sonnet-4-6", "max_llm_calls": 5, "max_tokens": 2048},
        llm_client=client,
    )


def _task() -> TaskInput:
    return TaskInput(task_id="t-1", workflow_id="wf-42", payload="do the thing")


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_workflow_task_acquires_lease_tagged_workflow_task() -> None:
    stub = _wallet_stub()
    agent = _agent(wallet=WalletClient(stub, backoff_base=0.0))

    output = await agent.handle(_task())

    assert output.status == TaskStatus.COMPLETED
    stub.AcquireLease.assert_awaited_once()
    req = stub.AcquireLease.await_args.args[0]
    assert req.cause == walletpb.CAUSE_WORKFLOW_TASK
    assert req.workflow_id == "wf-42"
    assert req.agent_id == "ember-owl"
    # The provider-reported actuals are settled back to the wallet.
    stub.SettleLease.assert_awaited_once()
    settle = stub.SettleLease.await_args.args[0]
    assert settle.actual_input_tokens == 12
    assert settle.actual_output_tokens == 34


async def test_budget_denial_is_a_structured_task_failure() -> None:
    stub = _wallet_stub(
        acquire=walletpb.LeaseResponse(
            denied=walletpb.LeaseDenied(
                scope="per_agent",
                spent_usd=10.0,
                limit_usd=10.0,
                message="per_agent budget exceeded",
            ),
        ),
    )
    agent = _agent(wallet=WalletClient(stub, backoff_base=0.0))

    output = await agent.handle(_task())

    assert output.status == TaskStatus.FAILED
    assert "budget exceeded" in output.result
    assert output.metadata["error_type"] == "budget_exceeded"
    assert output.metadata["budget_scope"] == "per_agent"
    # A denied lease must never reach the provider.
    agent._llm_client._provider.create_message.assert_not_awaited()  # type: ignore[union-attr]


async def test_no_wallet_leaves_the_workflow_path_unchanged() -> None:
    agent = _agent(wallet=None)

    output = await agent.handle(_task())

    assert output.status == TaskStatus.COMPLETED
    assert output.result == "done"
