"""RFC 0030 Layer 1 (v0.3.8) — the per-interaction cost ceiling, agent side.

Two behaviours pinned here:

* ``lease(interaction_id=…, interaction_budget_tokens=…)`` forwards both
  onto the ``LeaseRequest`` so the wallet can enforce the ceiling.
* An in-band ``LeaseDenied`` carrying
  ``LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED`` raises
  ``BudgetExceededError`` with ``reason="interaction_budget_exhausted"`` —
  fail-closed, exactly like a workflow-budget denial — while a plain budget
  denial (or an unset reason) still reads as ``"budget_exceeded"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.wallet_client import BudgetExceededError, WalletClient

pytestmark = pytest.mark.asyncio


def _grant() -> walletpb.LeaseResponse:
    return walletpb.LeaseResponse(
        grant=walletpb.LeaseGrant(
            lease_id="01J000000000000000000GRANT",
            granted_input_tokens=10,
            granted_output_tokens=20,
            ttl_seconds=60,
        ),
    )


def _stub(*, acquire: object = None) -> AsyncMock:
    stub = AsyncMock()
    stub.AcquireLease = AsyncMock(return_value=acquire or _grant())
    stub.SettleLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    stub.ReleaseLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    return stub


def _client(stub: AsyncMock) -> WalletClient:
    return WalletClient(stub, backoff_base=0.0)


async def test_lease_forwards_interaction_fields_onto_request() -> None:
    stub = _stub()
    client = _client(stub)

    async with client.lease(
        agent_id="iron-fox",
        model="m",
        estimated_input_tokens=10,
        estimated_max_output_tokens=20,
        cause=walletpb.CAUSE_CHANNEL_MESSAGE,
        interaction_id="int-1",
        interaction_budget_tokens=8000,
    ) as lease:
        lease.mark_call_started()

    req = stub.AcquireLease.await_args.args[0]
    assert req.interaction_id == "int-1"
    assert req.interaction_budget_tokens == 8000


async def test_lease_clamps_negative_budget_to_uncapped() -> None:
    stub = _stub()
    client = _client(stub)

    async with client.lease(
        agent_id="iron-fox",
        model="m",
        estimated_input_tokens=10,
        estimated_max_output_tokens=20,
        cause=walletpb.CAUSE_CHANNEL_MESSAGE,
        interaction_id="int-1",
        interaction_budget_tokens=-5,
    ):
        pass

    req = stub.AcquireLease.await_args.args[0]
    assert req.interaction_budget_tokens == 0, "a negative ceiling clamps to uncapped"


async def test_interaction_budget_denial_maps_to_typed_reason() -> None:
    denied = walletpb.LeaseResponse(
        denied=walletpb.LeaseDenied(
            scope="interaction",
            message='interaction "int-1" cost ceiling exceeded',
            reason=walletpb.LeaseDeniedReason.LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED,
        ),
    )
    stub = _stub(acquire=denied)
    client = _client(stub)

    with pytest.raises(BudgetExceededError) as excinfo:
        async with client.lease(
            agent_id="iron-fox",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_CHANNEL_MESSAGE,
            interaction_id="int-1",
            interaction_budget_tokens=4000,
        ):
            pytest.fail("lease body must not run when the lease is denied")

    assert excinfo.value.reason == "interaction_budget_exhausted"
    # Fail-closed: a denial precedes any lease — nothing to settle or release.
    stub.SettleLease.assert_not_awaited()
    stub.ReleaseLease.assert_not_awaited()


async def test_plain_budget_denial_still_maps_to_budget_exceeded() -> None:
    # An unset reason (proto default UNSPECIFIED) reads as the generic denial.
    denied = walletpb.LeaseResponse(
        denied=walletpb.LeaseDenied(scope="per_agent", message="per_agent budget exceeded"),
    )
    stub = _stub(acquire=denied)
    client = _client(stub)

    with pytest.raises(BudgetExceededError) as excinfo:
        async with client.lease(
            agent_id="iron-fox",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ):
            pytest.fail("lease body must not run when the lease is denied")

    assert excinfo.value.reason == "budget_exceeded"
