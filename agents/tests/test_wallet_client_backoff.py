"""Unit tests for RFC 0023 ``WalletClient._backoff`` — full-jitter retry delay.

Split out of ``test_wallet_client.py`` so both files stay within the repo's
file-size cap. ``_backoff`` reads only ``self._backoff_base`` and never
touches the gRPC stub, so these tests construct the client over a bare
``AsyncMock`` with no RPC wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from agents.wallet_client import WalletClient


def test_backoff_applies_full_jitter_within_exponential_bounds() -> None:
    """``_backoff`` draws *full jitter* in ``[0, base * 2**(attempt-1)]``.

    Jitter decorrelates retries when several of an agent process's
    concurrent tasks hit the same ``RESOURCE_EXHAUSTED`` active-lease cap
    and would otherwise back off in lockstep (RFC 0023 § F)."""
    client = WalletClient(AsyncMock(), backoff_base=0.1)

    for attempt, ceiling in [(1, 0.1), (2, 0.2), (3, 0.4)]:
        samples = [client._backoff(attempt) for _ in range(200)]
        assert all(0.0 <= s <= ceiling for s in samples), (
            f"attempt {attempt}: a backoff sample fell outside [0, {ceiling}]"
        )
        # Jitter must actually vary — a deterministic backoff would
        # collapse every draw to the same value.
        assert len(set(samples)) > 1, f"attempt {attempt}: no jitter applied"


def test_backoff_is_zero_when_base_is_zero() -> None:
    """A zero ``backoff_base`` collapses the jitter range to ``0.0`` — the
    fast-retry tuning the wallet test suite relies on stays deterministic."""
    client = WalletClient(AsyncMock(), backoff_base=0.0)
    assert client._backoff(1) == 0.0
    assert client._backoff(5) == 0.0
