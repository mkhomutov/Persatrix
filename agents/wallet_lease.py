"""What a wallet lease is: the grant a caller holds, and the refusal it gets instead.

Split from ``wallet_client.py`` (RFC 0009 Phases 3–4 plan, PR 7), which keeps
the gRPC client that asks for a lease and closes it. ``wallet_client`` still
exports both names, and RFC 0023 § B, § E and § F are explained there.

Like ``wallet_client``, this is an MIT leaf (RFC 0045): it imports no other
first-party module.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["BudgetExceededError", "Lease", "LeaseSettler"]


class LeaseSettler(Protocol):
    """The one thing a :class:`Lease` needs from the client that granted it."""

    async def _settle(self, lease_id: str, input_tokens: int, output_tokens: int) -> bool: ...


class BudgetExceededError(Exception):
    """The wallet refused to fund an LLM call.

    Raised for an in-band ``LeaseDenied`` (``reason="budget_exceeded"`` for an
    RFC 0023 per-scope denial, ``reason="interaction_budget_exhausted"`` for an
    RFC 0030 Layer 1 per-interaction cost-ceiling denial) and for a wallet that
    could not be reached (``reason="wallet_unreachable"``) — all block the call.
    The ``scope`` / ``spent_usd`` / ``limit_usd`` / ``estimated_usd`` fields
    mirror today's Go-side ``BudgetError`` shape and are populated only for the
    per-scope budget-denial case.
    """

    def __init__(
        self,
        message: str,
        *,
        scope: str = "",
        spent_usd: float = 0.0,
        limit_usd: float = 0.0,
        estimated_usd: float = 0.0,
        reason: str = "budget_exceeded",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.scope = scope
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.estimated_usd = estimated_usd
        self.reason = reason


class Lease:
    """A granted, in-flight lease yielded by :meth:`WalletClient.lease`.

    Callers settle via :meth:`settle` on the success path; the context
    manager handles the release / settle-at-granted exit paths. The
    granted token counts are retained so the context manager can close
    the lease pessimistically when a call happened but an explicit
    settle did not.
    """

    def __init__(
        self,
        client: LeaseSettler,
        lease_id: str,
        granted_input_tokens: int,
        granted_output_tokens: int,
        ttl_seconds: int,
    ) -> None:
        self._client = client
        self.lease_id = lease_id
        self.granted_input_tokens = granted_input_tokens
        self.granted_output_tokens = granted_output_tokens
        self.ttl_seconds = ttl_seconds
        # _call_started flips the exit path from release (the provider
        # was never contacted) to settle-at-granted (it may have been) —
        # the context manager makes this disambiguation on *both* the
        # exception and the clean-exit-without-settle paths. _settled
        # makes settle idempotent and tells the context manager whether
        # a clean exit still needs a defensive close.
        self._call_started = False
        self._settled = False

    def mark_call_started(self) -> None:
        """Record that the LLM provider call is about to be issued.

        Call this immediately before the provider request. It selects the
        pessimistic *settle-at-granted* exit over *release* when the lease
        block then exits without an explicit settle — whether by raising
        or by a clean return. Once the provider has been contacted, real
        spend may have occurred (RFC 0023 § F)."""
        self._call_started = True

    async def settle(self, *, input_tokens: int, output_tokens: int) -> None:
        """Reconcile the provisional charge against the provider actuals.

        Idempotent: a second call is a no-op, so the context manager's
        defensive close does not double-settle. A settlement RPC that
        fails after all retries is swallowed — the orchestrator reaper
        reconciles the lease at the granted amount (RFC 0023 § F), and a
        failed settle must never lose a successful LLM response."""
        if self._settled:
            return
        self._settled = True
        await self._client._settle(self.lease_id, input_tokens, output_tokens)
