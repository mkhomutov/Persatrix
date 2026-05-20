"""Unit tests for RFC 0023 PR 3 — ``LLMClient.create_message`` lease wrapping.

When an :class:`~agents.wallet_client.WalletClient` is attached and a
non-``UNSPECIFIED`` ``cause`` is supplied, ``create_message`` brackets the
provider call in a wallet lease: acquire → provider call → settle with the
provider-reported actuals. Without a wallet, or without a ``cause``, the
call is unchanged — that is the un-migrated v0.2.3 path PRs 4–6 wire.

The wallet is exercised through the real :class:`WalletClient` over a
mocked gRPC stub; the LLM provider is mocked at the boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import BudgetExceededError, LLMClient, LLMResponse, StopReason, Usage
from agents.observability.spans import LLM_CALL_SPAN
from agents.wallet_client import WalletClient

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _response(*, input_tokens: int = 40, output_tokens: int = 90) -> LLMResponse:
    return LLMResponse(
        text="ok",
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _wallet_stub(*, acquire: object = None) -> AsyncMock:
    """A mock ``WalletServiceStub`` granting leases by default."""
    stub = AsyncMock()
    stub.AcquireLease = AsyncMock(
        return_value=acquire or walletpb.LeaseResponse(
            grant=walletpb.LeaseGrant(
                lease_id="01J0000000000000000LEASE",
                granted_input_tokens=500,
                granted_output_tokens=500,
                ttl_seconds=60,
            ),
        ),
    )
    stub.SettleLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    stub.ReleaseLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    return stub


def _provider(response: LLMResponse | None = None) -> AsyncMock:
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=response or _response())
    return provider


_CALL_KWARGS: dict[str, Any] = {
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "hello"}],
    "system": "be helpful",
    "tools": [],
    "max_tokens": 256,
    "temperature": 0.3,
}


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """An InMemorySpanExporter wired into the active tracer provider."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


# ─── Leased path ──────────────────────────────────────────────────────────────


async def test_create_message_leases_and_settles_with_actuals() -> None:
    stub = _wallet_stub()
    wallet = WalletClient(stub, backoff_base=0.0)
    provider = _provider(_response(input_tokens=37, output_tokens=88))
    client = LLMClient(provider, wallet=wallet)

    response = await client.create_message(
        cause=walletpb.CAUSE_WORKFLOW_TASK,
        workflow_id="wf-9",
        agent_id="ember-owl",
        **_CALL_KWARGS,
    )

    assert response.usage.output_tokens == 88
    stub.AcquireLease.assert_awaited_once()
    acquire_req = stub.AcquireLease.await_args.args[0]
    assert acquire_req.cause == walletpb.CAUSE_WORKFLOW_TASK
    assert acquire_req.agent_id == "ember-owl"
    assert acquire_req.workflow_id == "wf-9"
    # The lease must carry a non-zero input estimate from the tokeniser.
    assert acquire_req.estimated_input_tokens > 0
    assert acquire_req.estimated_max_output_tokens == 256

    stub.SettleLease.assert_awaited_once()
    settle_req = stub.SettleLease.await_args.args[0]
    assert settle_req.actual_input_tokens == 37
    assert settle_req.actual_output_tokens == 88
    provider.create_message.assert_awaited_once()


async def test_create_message_sets_lease_id_span_attribute(
    exporter: InMemorySpanExporter,
) -> None:
    wallet = WalletClient(_wallet_stub(), backoff_base=0.0)
    client = LLMClient(_provider(), wallet=wallet)

    await client.create_message(
        cause=walletpb.CAUSE_WORKFLOW_TASK,
        agent_id="ember-owl",
        **_CALL_KWARGS,
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == LLM_CALL_SPAN]
    assert spans, "expected an agent.llm.call span"
    attrs = spans[-1].attributes
    assert attrs is not None
    assert attrs["persatrix.lease_id"] == "01J0000000000000000LEASE"


async def test_create_message_settles_at_granted_on_provider_error() -> None:
    stub = _wallet_stub()
    provider = _provider()
    provider.create_message = AsyncMock(side_effect=RuntimeError("provider 5xx"))
    client = LLMClient(provider, wallet=WalletClient(stub, backoff_base=0.0))

    with pytest.raises(RuntimeError, match="provider 5xx"):
        await client.create_message(
            cause=walletpb.CAUSE_WORKFLOW_TASK,
            agent_id="ember-owl",
            **_CALL_KWARGS,
        )

    # The provider call started, then raised — close pessimistically at the
    # granted amount, not release.
    stub.SettleLease.assert_awaited_once()
    settle_req = stub.SettleLease.await_args.args[0]
    assert settle_req.actual_input_tokens == 500
    assert settle_req.actual_output_tokens == 500
    stub.ReleaseLease.assert_not_awaited()


async def test_create_message_propagates_budget_denial() -> None:
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
    provider = _provider()
    client = LLMClient(provider, wallet=WalletClient(stub, backoff_base=0.0))

    with pytest.raises(BudgetExceededError) as excinfo:
        await client.create_message(
            cause=walletpb.CAUSE_WORKFLOW_TASK,
            agent_id="ember-owl",
            **_CALL_KWARGS,
        )

    assert excinfo.value.scope == "per_agent"
    # A denied lease must never reach the provider.
    provider.create_message.assert_not_awaited()


# ─── Un-leased path (no wallet / no cause) ────────────────────────────────────


async def test_create_message_skips_lease_when_no_cause() -> None:
    stub = _wallet_stub()
    provider = _provider()
    client = LLMClient(provider, wallet=WalletClient(stub, backoff_base=0.0))

    # cause defaults to CAUSE_UNSPECIFIED — the call paths PRs 4–6 wire are
    # not leased yet, so create_message behaves exactly as v0.2.3.
    await client.create_message(**_CALL_KWARGS)

    stub.AcquireLease.assert_not_awaited()
    provider.create_message.assert_awaited_once()


async def test_create_message_skips_lease_when_no_wallet() -> None:
    provider = _provider()
    client = LLMClient(provider)  # no wallet attached

    response = await client.create_message(
        cause=walletpb.CAUSE_WORKFLOW_TASK,
        **_CALL_KWARGS,
    )

    assert response.text == "ok"
    provider.create_message.assert_awaited_once()


async def test_set_wallet_attaches_wallet_post_construction() -> None:
    stub = _wallet_stub()
    client = LLMClient(_provider())  # constructed before the channel exists
    client.set_wallet(WalletClient(stub, backoff_base=0.0))

    await client.create_message(
        cause=walletpb.CAUSE_WORKFLOW_TASK,
        agent_id="ember-owl",
        **_CALL_KWARGS,
    )

    stub.AcquireLease.assert_awaited_once()
