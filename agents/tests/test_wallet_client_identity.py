"""The wallet client stamps its agent identity on the wire (ISSUE-0111).

The orchestrator's RFC 0009 rate limiter keys per-agent budgets on the
``x-agent-id`` gRPC metadata (``internal/security/middleware.go``,
``AgentIDMetadataKey``). A wallet RPC without it lands in the single shared
anonymous bucket — so the whole fleet's wallet traffic competed for one
60-calls/60s window, and the RFC 0052 bounded-close summary fan (N personas
leasing near-simultaneously) starved late callers into the
``[interaction summary unavailable]`` placeholder, violating §D. Every
wallet RPC must therefore carry the hosting agent's id as ``x-agent-id``.

The generated ``WalletServiceStub`` is mocked at the boundary — no real
network, per the TDD rule in ``.github/copilot-instructions.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.wallet_client import WalletClient

pytestmark = pytest.mark.asyncio


def _grant() -> walletpb.LeaseResponse:
    return walletpb.LeaseResponse(
        grant=walletpb.LeaseGrant(
            lease_id="01J00000000000000000IDENT",
            granted_input_tokens=100,
            granted_output_tokens=200,
            ttl_seconds=60,
        ),
    )


def _stub() -> AsyncMock:
    stub = AsyncMock()
    stub.AcquireLease = AsyncMock(return_value=_grant())
    stub.SettleLease = AsyncMock(
        return_value=walletpb.SettlementAck(success=True),
    )
    stub.ReleaseLease = AsyncMock(
        return_value=walletpb.SettlementAck(success=True),
    )
    return stub


def _metadata_kwarg(mock: AsyncMock) -> tuple[tuple[str, str], ...]:
    assert mock.await_args is not None
    metadata = mock.await_args.kwargs.get("metadata")
    return tuple(metadata) if metadata is not None else ()


async def test_acquire_and_settle_carry_x_agent_id() -> None:
    stub = _stub()
    client = WalletClient(stub, agent_id="ember-owl", backoff_base=0.0)

    async with client.lease(
        agent_id="ember-owl",
        model="m",
        estimated_input_tokens=1,
        estimated_max_output_tokens=1,
        cause=walletpb.CAUSE_CHANNEL_MESSAGE,
    ) as lease:
        lease.mark_call_started()
        await lease.settle(input_tokens=1, output_tokens=1)

    assert ("x-agent-id", "ember-owl") in tuple(_metadata_kwarg(stub.AcquireLease))
    assert ("x-agent-id", "ember-owl") in tuple(_metadata_kwarg(stub.SettleLease))


async def test_release_carries_x_agent_id() -> None:
    stub = _stub()
    client = WalletClient(stub, agent_id="iron-fox", backoff_base=0.0)

    with pytest.raises(RuntimeError, match="boom"):
        async with client.lease(
            agent_id="iron-fox",
            model="m",
            estimated_input_tokens=1,
            estimated_max_output_tokens=1,
            cause=walletpb.CAUSE_CHANNEL_MESSAGE,
        ):
            raise RuntimeError("boom")  # pre-call abort → ReleaseLease

    assert ("x-agent-id", "iron-fox") in tuple(_metadata_kwarg(stub.ReleaseLease))


async def test_no_agent_id_sends_no_metadata() -> None:
    """Backward-compat: an identity-less client keeps the bare call shape."""
    stub = _stub()
    client = WalletClient(stub, backoff_base=0.0)

    async with client.lease(
        agent_id="ember-owl",
        model="m",
        estimated_input_tokens=1,
        estimated_max_output_tokens=1,
        cause=walletpb.CAUSE_CHANNEL_MESSAGE,
    ) as lease:
        lease.mark_call_started()
        await lease.settle(input_tokens=1, output_tokens=1)

    assert stub.AcquireLease.await_args.kwargs.get("metadata") is None
    assert stub.SettleLease.await_args.kwargs.get("metadata") is None


async def test_from_channel_forwards_agent_id() -> None:
    channel = AsyncMock()
    client = WalletClient.from_channel(channel, agent_id="nova-sparrow")
    assert client._metadata is not None
    assert ("x-agent-id", "nova-sparrow") in tuple(client._metadata)
