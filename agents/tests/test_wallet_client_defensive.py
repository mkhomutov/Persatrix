"""RFC 0023 — defensive-path coverage for ``WalletClient`` (PR #385 review follow-up).

Two robustness pins the original PR's test matrix does not cover:

* **Unknown ``LeaseResponse.outcome`` oneof** — the proto declares ``grant``
  and ``denied`` arms today, but ``WhichOneof("outcome")`` also returns
  ``None`` when the field is unset (proto default) and would return a new
  arm name on a future proto evolution. Both cases must fail loudly rather
  than silently yielding a zero-valued ``Lease`` (empty ``lease_id``, zero
  ceilings) that would propagate into settle/release.
* **Concurrent leases on one ``WalletClient``** — the client is designed to
  be per-call independent (no shared mutable state across leases on a
  single instance), but no test pins that today; a future refactor that
  introduces shared in-flight state would regress silently.

Split into its own file rather than appended to ``test_wallet_client.py``
to keep both within the repo's 500-line cap — same pattern as the
``_backoff`` and ``_cleanup`` test files.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.wallet_client import WalletClient

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _grant(
    *,
    lease_id: str,
    granted_input_tokens: int = 100,
    granted_output_tokens: int = 200,
    ttl_seconds: int = 60,
) -> walletpb.LeaseResponse:
    """A positive ``AcquireLease`` outcome with a caller-chosen lease_id."""
    return walletpb.LeaseResponse(
        grant=walletpb.LeaseGrant(
            lease_id=lease_id,
            granted_input_tokens=granted_input_tokens,
            granted_output_tokens=granted_output_tokens,
            ttl_seconds=ttl_seconds,
        ),
    )


def _client(stub: AsyncMock) -> WalletClient:
    """A WalletClient over *stub* with deterministic, fast retry tuning."""
    return WalletClient(stub, backoff_base=0.0)


# ─── Unknown / unset outcome oneof ────────────────────────────────────────────


async def test_acquire_unset_outcome_oneof_raises_rather_than_silently_granting() -> None:
    """A ``LeaseResponse`` with no oneof arm set must not be treated as a grant.

    The default-constructed message has ``WhichOneof("outcome") is None`` —
    a wire-level malformation, a proto-evolution mismatch, or a server bug.
    The previous fall-through would build a ``Lease("", 0, 0, 0)`` and
    propagate an empty ``lease_id`` into ``_settle`` / ``_release``. The
    agent's per-call lease must fail loudly so the workflow surfaces it
    as a generic provider error rather than a silent budget bypass.
    """
    stub = AsyncMock()
    # No grant=, no denied= — WhichOneof("outcome") returns None.
    stub.AcquireLease = AsyncMock(return_value=walletpb.LeaseResponse())
    stub.SettleLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    stub.ReleaseLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    client = _client(stub)

    with pytest.raises(RuntimeError, match="outcome"):
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ):
            pytest.fail("lease body must not run on a malformed response")

    # No lease was actually granted, so there is nothing to settle or release.
    stub.SettleLease.assert_not_awaited()
    stub.ReleaseLease.assert_not_awaited()


# ─── Concurrent leases on one WalletClient ────────────────────────────────────


async def test_concurrent_leases_have_independent_state() -> None:
    """Two concurrent ``lease()`` contexts on the same client are independent.

    The wallet client is designed to be per-call independent — the
    mutable state (``_call_started`` / ``_settled``) lives on the
    ``Lease`` object the context manager yields, not on the client. This
    test pins that contract: a future refactor that introduces shared
    in-flight state on the client (e.g., a single "current lease" attr)
    would corrupt one lease's bookkeeping from the other and is caught
    here rather than in production.
    """
    stub = AsyncMock()
    # Distinct grants on successive AcquireLease calls — the order in which
    # the two concurrent tasks pull from the side_effect iterator is the
    # asyncio scheduler's, not the test's, so the assertions are set-based.
    stub.AcquireLease = AsyncMock(side_effect=[
        _grant(lease_id="lease-A", granted_input_tokens=100, granted_output_tokens=100),
        _grant(lease_id="lease-B", granted_input_tokens=200, granted_output_tokens=200),
    ])
    stub.SettleLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    stub.ReleaseLease = AsyncMock(return_value=walletpb.SettlementAck(success=True))
    client = _client(stub)

    async def _run_one(input_t: int, output_t: int) -> str:
        async with client.lease(
            agent_id="ember-owl",
            model="m",
            estimated_input_tokens=10,
            estimated_max_output_tokens=20,
            cause=walletpb.CAUSE_WORKFLOW_TASK,
        ) as lease:
            lease.mark_call_started()
            await lease.settle(input_tokens=input_t, output_tokens=output_t)
            return lease.lease_id

    a, b = await asyncio.gather(_run_one(11, 22), _run_one(33, 44))

    # Each task saw a distinct grant — Lease state did not leak across calls.
    assert {a, b} == {"lease-A", "lease-B"}
    assert stub.AcquireLease.await_count == 2
    assert stub.SettleLease.await_count == 2

    # Each lease settled with its own actuals (not the sibling's).
    settle_pairs = {
        (call.args[0].actual_input_tokens, call.args[0].actual_output_tokens)
        for call in stub.SettleLease.await_args_list
    }
    assert settle_pairs == {(11, 22), (33, 44)}
    # Both settled cleanly — no defensive close was needed.
    stub.ReleaseLease.assert_not_awaited()
