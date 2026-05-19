"""RFC 0023 — clean-exit cleanup-path robustness for ``WalletClient.lease``.

When a lease block exits *cleanly* (no exception) without an explicit
``settle``, the context manager performs a defensive close — ``release`` if
the provider was never contacted, ``settle-at-granted`` if it was. That
defensive close is *best-effort*, symmetric with the exception-path cleanup
guard: an unexpected error inside it must not turn a clean, successful exit
into a failure. A swallowed cleanup error leaves the lease for the
orchestrator reaper to reconcile at TTL expiry (RFC 0023 § F).

This is the clean-exit counterpart of the exception-path cleanup tests in
``test_wallet_client.py`` (``test_release_cleanup_failure_…`` /
``test_settle_at_granted_cleanup_failure_…``). Split into its own file so
both stay within the repo's 500-line file-size cap — same reason as
``test_wallet_client_backoff.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from agents.generated import wallet_pb2 as walletpb
from agents.wallet_client import WalletClient

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _granting_stub() -> AsyncMock:
    """A mock ``WalletServiceStub`` that grants leases and acks settle/release."""
    stub = AsyncMock()
    stub.AcquireLease = AsyncMock(
        return_value=walletpb.LeaseResponse(
            grant=walletpb.LeaseGrant(
                lease_id="01J0000000000000000CLEAN",
                granted_input_tokens=100,
                granted_output_tokens=200,
                ttl_seconds=60,
            ),
        ),
    )
    stub.SettleLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    stub.ReleaseLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    return stub


def _client(stub: AsyncMock) -> WalletClient:
    """A WalletClient over *stub* with deterministic, fast retry tuning."""
    return WalletClient(stub, backoff_base=0.0)


# ─── clean-exit cleanup failure is swallowed, not propagated ──────────────────


async def test_clean_exit_settle_at_granted_cleanup_failure_is_swallowed() -> None:
    """A clean exit *after* ``mark_call_started`` closes the lease at the
    granted amount. If that defensive settle hits an *unexpected* error —
    ``_settle`` swallows ``AioRpcError`` but not arbitrary failures — it must
    be swallowed: the block exited successfully, and the reaper reconciles
    the lease at TTL expiry. The exception-path guard already does this; the
    clean-exit path must be symmetric."""
    stub = _granting_stub()
    # A plain RuntimeError stands in for an unexpected bug in the cleanup
    # path, which _settle does not catch.
    stub.SettleLease = AsyncMock(side_effect=RuntimeError("cleanup boom"))
    client = _client(stub)

    # No exception must escape the `async with` — a clean exit stays clean.
    async with client.lease(
        agent_id="ember-owl",
        model="m",
        estimated_input_tokens=10,
        estimated_max_output_tokens=20,
        cause=walletpb.CAUSE_WORKFLOW_TASK,
    ) as lease:
        lease.mark_call_started()  # call happened; no explicit settle()

    # The defensive close was attempted; its failure was swallowed.
    stub.SettleLease.assert_awaited_once()


async def test_clean_exit_release_cleanup_failure_is_swallowed() -> None:
    """The no-call clean-exit variant: a body that exits cleanly *before*
    ``mark_call_started`` reverses the hold via ``release``. An unexpected
    error in that defensive release is likewise swallowed rather than
    failing the block."""
    stub = _granting_stub()
    stub.ReleaseLease = AsyncMock(side_effect=RuntimeError("cleanup boom"))
    client = _client(stub)

    async with client.lease(
        agent_id="ember-owl",
        model="m",
        estimated_input_tokens=10,
        estimated_max_output_tokens=20,
        cause=walletpb.CAUSE_WORKFLOW_TASK,
    ) as lease:
        assert lease is not None  # no mark_call_started(), no settle()

    stub.ReleaseLease.assert_awaited_once()
