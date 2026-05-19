"""Unit tests for the RFC 0023 Python wallet client (PR 3).

Covers the four ``WalletClient.lease()`` exit paths and the acquire-side
gRPC-status branching:

* settle           — caller settles with provider actuals on the normal path
* release          — exception *before* the LLM call reverses the provisional
* settle-at-granted — exception *after* the call closes the lease pessimistically
* retry            — a transient ``SettleLease`` failure is retried with backoff
* LeaseDenied      — an in-band budget denial raises ``BudgetExceededError``
* wallet-unreachable — a ``UNAVAILABLE`` status fails *closed* (RFC 0023 § F)
* ResourceExhausted — the per-agent cap is transient: retried, then surfaced
                      distinctly from a hard budget failure
* InvalidArgument  — a server-/agent-side bug fails loudly without retry

The generated ``WalletServiceStub`` is mocked at the boundary — no real
network, per the TDD rule in ``.github/copilot-instructions.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import grpc
import grpc.aio
import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.wallet_client import BudgetExceededError, Lease, WalletClient

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _grant(
    *,
    lease_id: str = "01J000000000000000000GRANT",
    granted_input_tokens: int = 100,
    granted_output_tokens: int = 200,
    ttl_seconds: int = 60,
) -> walletpb.LeaseResponse:
    """A positive ``AcquireLease`` outcome."""
    return walletpb.LeaseResponse(
        grant=walletpb.LeaseGrant(
            lease_id=lease_id,
            granted_input_tokens=granted_input_tokens,
            granted_output_tokens=granted_output_tokens,
            ttl_seconds=ttl_seconds,
        ),
    )


def _denied(
    *,
    scope: str = "per_agent",
    spent_usd: float = 9.5,
    limit_usd: float = 10.0,
    estimated_usd: float = 1.2,
    message: str = "per_agent budget exceeded",
) -> walletpb.LeaseResponse:
    """A budget-denial ``AcquireLease`` outcome (in-band oneof arm)."""
    return walletpb.LeaseResponse(
        denied=walletpb.LeaseDenied(
            scope=scope,
            spent_usd=spent_usd,
            limit_usd=limit_usd,
            estimated_usd=estimated_usd,
            message=message,
        ),
    )


def _rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    """Construct an ``AioRpcError`` carrying *code* for stub side-effects."""
    return grpc.aio.AioRpcError(
        code, grpc.aio.Metadata(), grpc.aio.Metadata(), details=f"simulated {code}",
    )


def _stub(
    *,
    acquire: object = None,
    settle: object = None,
    release: object = None,
) -> AsyncMock:
    """A mock ``WalletServiceStub`` with the three unary RPCs as AsyncMocks."""
    stub = AsyncMock()
    stub.AcquireLease = AsyncMock(return_value=acquire or _grant())
    stub.SettleLease = AsyncMock(
        return_value=settle or walletpb.SettlementAck(success=True),
    )
    stub.ReleaseLease = AsyncMock(
        return_value=release or walletpb.SettlementAck(success=True),
    )
    return stub


def _client(stub: AsyncMock) -> WalletClient:
    """A WalletClient over *stub* with deterministic, fast retry tuning."""
    return WalletClient(stub, backoff_base=0.0)


# ─── lease() exit path 1: settle ──────────────────────────────────────────────


async def test_lease_settle_path_records_provider_actuals() -> None:
    stub = _stub()
    client = _client(stub)

    async with client.lease(
        agent_id="ember-owl",
        model="claude-sonnet-4-6",
        estimated_input_tokens=80,
        estimated_max_output_tokens=200,
        cause=walletpb.CAUSE_WORKFLOW_TASK,
        workflow_id="wf-1",
    ) as lease:
        assert isinstance(lease, Lease)
        assert lease.lease_id == "01J000000000000000000GRANT"
        lease.mark_call_started()
        await lease.settle(input_tokens=72, output_tokens=143)

    stub.AcquireLease.assert_awaited_once()
    acquire_req = stub.AcquireLease.await_args.args[0]
    assert acquire_req.agent_id == "ember-owl"
    assert acquire_req.cause == walletpb.CAUSE_WORKFLOW_TASK
    assert acquire_req.workflow_id == "wf-1"

    stub.SettleLease.assert_awaited_once()
    settle_req = stub.SettleLease.await_args.args[0]
    assert settle_req.lease_id == "01J000000000000000000GRANT"
    assert settle_req.actual_input_tokens == 72
    assert settle_req.actual_output_tokens == 143
    stub.ReleaseLease.assert_not_awaited()


async def test_lease_settle_is_idempotent_on_double_call() -> None:
    stub = _stub()
    client = _client(stub)

    async with client.lease(
        agent_id="ember-owl",
        model="m",
        estimated_input_tokens=10,
        estimated_max_output_tokens=20,
        cause=walletpb.CAUSE_WORKFLOW_TASK,
    ) as lease:
        lease.mark_call_started()
        await lease.settle(input_tokens=5, output_tokens=6)
        await lease.settle(input_tokens=999, output_tokens=999)

    # A second settle is a no-op — the first one already closed the lease.
    stub.SettleLease.assert_awaited_once()


# ─── lease() exit path 2: release (exception before the LLM call) ─────────────


async def test_lease_releases_on_exception_before_call() -> None:
    stub = _stub()
    client = _client(stub)

    with pytest.raises(RuntimeError, match="prep failed"):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            assert lease is not None
            raise RuntimeError("prep failed")  # before mark_call_started()

    stub.ReleaseLease.assert_awaited_once()
    release_req = stub.ReleaseLease.await_args.args[0]
    assert release_req.lease_id == "01J000000000000000000GRANT"
    assert release_req.reason == "aborted"
    stub.SettleLease.assert_not_awaited()


# ─── lease() exit path 3: settle-at-granted (exception after the LLM call) ────


async def test_lease_settles_at_granted_on_exception_after_call() -> None:
    stub = _stub(acquire=_grant(granted_input_tokens=111, granted_output_tokens=222))
    client = _client(stub)

    with pytest.raises(RuntimeError, match="provider 5xx"):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=111,
            estimated_max_output_tokens=222,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            lease.mark_call_started()
            raise RuntimeError("provider 5xx")  # after the call started

    stub.SettleLease.assert_awaited_once()
    settle_req = stub.SettleLease.await_args.args[0]
    # Pessimistic: settle at the granted (worst-case) amount.
    assert settle_req.actual_input_tokens == 111
    assert settle_req.actual_output_tokens == 222
    stub.ReleaseLease.assert_not_awaited()


async def test_lease_settles_at_granted_on_clean_exit_without_settle() -> None:
    """Defensive: a caller that forgets to settle still closes the lease."""
    stub = _stub(acquire=_grant(granted_input_tokens=30, granted_output_tokens=40))
    client = _client(stub)

    async with client.lease(
        agent_id="ember-owl",
        model="m",
        estimated_input_tokens=30,
        estimated_max_output_tokens=40,
        cause=walletpb.CAUSE_WORKFLOW_TASK,
    ) as lease:
        lease.mark_call_started()  # call happened, but settle() never called

    stub.SettleLease.assert_awaited_once()
    settle_req = stub.SettleLease.await_args.args[0]
    assert settle_req.actual_input_tokens == 30
    assert settle_req.actual_output_tokens == 40


# ─── exception-path cleanup must not mask the caller's exception ──────────────


async def test_release_cleanup_failure_does_not_mask_original_exception() -> None:
    """An unexpected error in the release cleanup must not replace the
    exception the caller raised.

    ``_release`` swallows ``AioRpcError`` but not arbitrary failures; an
    unexpected error escaping it on the exception exit path would otherwise
    replace the original error, hiding a budget-vs-provider failure the
    agent needs to surface (RFC 0023 § F)."""
    stub = _stub()
    # ReleaseLease raises a plain RuntimeError — a stand-in for an
    # unexpected bug in the cleanup path, which _release does not catch.
    stub.ReleaseLease = AsyncMock(side_effect=RuntimeError("cleanup boom"))
    client = _client(stub)

    with pytest.raises(ValueError, match="caller error"):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            assert lease is not None
            raise ValueError("caller error")  # before mark_call_started()

    # Cleanup was attempted; its failure was swallowed, not propagated.
    stub.ReleaseLease.assert_awaited_once()


async def test_settle_at_granted_cleanup_failure_does_not_mask_original() -> None:
    """Same guard on the settle-at-granted exit path: an unexpected error in
    the settle cleanup must not replace the post-call exception the caller
    raised."""
    stub = _stub()
    # SettleLease raises a plain RuntimeError — _settle catches AioRpcError
    # only, so an unexpected error escapes its retry loop.
    stub.SettleLease = AsyncMock(side_effect=RuntimeError("cleanup boom"))
    client = _client(stub)

    with pytest.raises(ValueError, match="caller error"):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            lease.mark_call_started()
            raise ValueError("caller error")  # after the call started

    stub.SettleLease.assert_awaited_once()


# ─── lease() exit path 4: retry on a transient settle failure ─────────────────


async def test_settle_retries_transient_failure_then_succeeds() -> None:
    stub = _stub()
    stub.SettleLease = AsyncMock(side_effect=[
        _rpc_error(grpc.StatusCode.UNAVAILABLE),
        _rpc_error(grpc.StatusCode.UNAVAILABLE),
        walletpb.SettlementAck(success=True),
    ])
    client = _client(stub)

    with patch("agents.wallet_client.asyncio.sleep", new=AsyncMock()):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            lease.mark_call_started()
            await lease.settle(input_tokens=5, output_tokens=6)

    assert stub.SettleLease.await_count == 3


async def test_settle_swallows_failure_after_retries_exhausted() -> None:
    """A settle that never succeeds must not lose the LLM response — the
    reaper reconciles the lease at the granted amount (RFC 0023 § F)."""
    stub = _stub()
    stub.SettleLease = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAVAILABLE))
    client = _client(stub)

    with patch("agents.wallet_client.asyncio.sleep", new=AsyncMock()):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            lease.mark_call_started()
            await lease.settle(input_tokens=5, output_tokens=6)  # must not raise

    assert stub.SettleLease.await_count == 3


# ─── acquire branching: in-band LeaseDenied ───────────────────────────────────


async def test_acquire_denial_raises_budget_exceeded_with_fields() -> None:
    stub = _stub(acquire=_denied(scope="global", spent_usd=42.0, limit_usd=40.0))
    client = _client(stub)

    with pytest.raises(BudgetExceededError) as excinfo:
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ):
            pytest.fail("lease body must not run when the lease is denied")

    err = excinfo.value
    assert err.scope == "global"
    assert err.spent_usd == 42.0
    assert err.limit_usd == 40.0
    assert err.reason == "budget_exceeded"
    # A denial happens before any lease exists — nothing to settle or release.
    stub.SettleLease.assert_not_awaited()
    stub.ReleaseLease.assert_not_awaited()


# ─── acquire branching: wallet unreachable → fail closed ──────────────────────


async def test_acquire_unavailable_fails_closed_as_budget_error() -> None:
    stub = _stub()
    stub.AcquireLease = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAVAILABLE))
    client = _client(stub)

    with pytest.raises(BudgetExceededError) as excinfo:
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ):
            pytest.fail("lease body must not run when the wallet is unreachable")

    assert excinfo.value.reason == "wallet_unreachable"


# ─── acquire branching: ResourceExhausted is transient ────────────────────────


async def test_acquire_resource_exhausted_retries_then_surfaces_distinctly() -> None:
    stub = _stub()
    stub.AcquireLease = AsyncMock(
        side_effect=_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED),
    )
    client = _client(stub)

    # The per-agent active-lease cap is a transient condition — a cap slot
    # frees as sibling leases settle — so it is retried, then surfaced as the
    # raw gRPC error, NOT collapsed into a hard BudgetExceededError.
    with patch("agents.wallet_client.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            async with client.lease(
                agent_id="ember-owl",
                model="m",
                estimated_input_tokens=10,
                estimated_max_output_tokens=20,
                cause=walletpb.CAUSE_WORKFLOW_TASK,
            ):
                pytest.fail("lease body must not run when acquisition fails")

    assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert stub.AcquireLease.await_count == 3
    assert not isinstance(excinfo.value, BudgetExceededError)


async def test_acquire_resource_exhausted_recovers_within_retries() -> None:
    stub = _stub()
    stub.AcquireLease = AsyncMock(side_effect=[
        _rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED),
        _grant(),
    ])
    client = _client(stub)

    with patch("agents.wallet_client.asyncio.sleep", new=AsyncMock()):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            lease.mark_call_started()
            await lease.settle(input_tokens=1, output_tokens=2)

    assert stub.AcquireLease.await_count == 2
    stub.SettleLease.assert_awaited_once()


# ─── acquire branching: InvalidArgument fails loudly, no retry ────────────────


async def test_acquire_invalid_argument_raises_loud_without_retry() -> None:
    stub = _stub()
    stub.AcquireLease = AsyncMock(
        side_effect=_rpc_error(grpc.StatusCode.INVALID_ARGUMENT),
    )
    client = _client(stub)

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ):
            pytest.fail("lease body must not run on a malformed request")

    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    # A server-/agent-side bug must surface immediately, not be retried.
    stub.AcquireLease.assert_awaited_once()
    assert not isinstance(excinfo.value, BudgetExceededError)
