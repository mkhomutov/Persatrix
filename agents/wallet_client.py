"""RFC 0023 — Python wallet client (PR 3).

Every LLM invocation acquires a server-issued lease from the
orchestrator-side ``WalletService`` before issuing, and settles actual
usage afterward. This module wraps the generated gRPC stub and exposes a
*single* public surface — the :meth:`WalletClient.lease` async context
manager — so "every call path brackets its LLM call" is enforced by the
shape of the API rather than by reviewer vigilance.

Lease lifecycle (RFC 0023 § B / § E / § F)
------------------------------------------
``lease()`` acquires on enter and, on exit, resolves the provisional
charge one of four ways:

* **settle** — the caller invoked :meth:`Lease.settle` with the
  provider-reported actuals on the normal path.
* **release** — the block raised *before* the LLM call started
  (:meth:`Lease.mark_call_started` was never reached): the provisional
  charge is fully reversed.
* **settle-at-granted** — the block raised *after* the call started, or
  exited cleanly without an explicit settle: the lease closes at the
  granted (worst-case) amount. Pessimistic — an in-flight provider
  request may have completed and spent real budget.
* the orchestrator-side **reaper** is the backstop — a settle that never
  reaches the wallet is reconciled at the granted amount on TTL expiry,
  so a dropped settlement over-accounts but never loses spend.

Failure modes (RFC 0023 § F)
----------------------------
* a budget **denial** (in-band ``LeaseDenied``) raises
  :class:`BudgetExceededError` — the lease body never runs.
* the wallet **unreachable** (``UNAVAILABLE`` / ``DEADLINE_EXCEEDED``)
  also raises :class:`BudgetExceededError` (``reason="wallet_unreachable"``):
  an agent that cannot reach the wallet cannot prove it has budget, so
  the call fails *closed*.
* a ``RESOURCE_EXHAUSTED`` status — the per-agent active-lease cap — is
  *transient* (a slot frees as sibling leases settle): it is retried
  with backoff and, if still failing, surfaced as the raw gRPC error,
  distinct from a hard budget failure.
* ``INTERNAL`` / ``INVALID_ARGUMENT`` indicate a server- or agent-side
  bug and are re-raised immediately, without retry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import grpc.aio

from .generated import wallet_pb2 as walletpb
from .generated import wallet_pb2_grpc as wallet_grpc

logger = logging.getLogger(__name__)

__all__ = ["BudgetExceededError", "Lease", "WalletClient"]

# gRPC statuses that mean "the wallet could not be reached / could not
# answer in time" — both fail closed (RFC 0023 § F).
_UNREACHABLE_CODES: frozenset[grpc.StatusCode] = frozenset({
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
})


class BudgetExceededError(Exception):
    """The wallet refused to fund an LLM call.

    Raised for an in-band ``LeaseDenied`` (``reason="budget_exceeded"``)
    and for a wallet that could not be reached (``reason="wallet_unreachable"``)
    — both block the call. The ``scope`` / ``spent_usd`` / ``limit_usd`` /
    ``estimated_usd`` fields mirror today's Go-side ``BudgetError`` shape
    and are populated only for the budget-denial case.
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
    the lease pessimistically when an explicit settle did not happen.
    """

    def __init__(
        self,
        client: WalletClient,
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
        # _call_started flips the exception exit path from release (the
        # provider was never contacted) to settle-at-granted (it may have
        # been). _settled makes settle idempotent and tells the context
        # manager whether a clean exit still needs a defensive close.
        self._call_started = False
        self._settled = False

    def mark_call_started(self) -> None:
        """Record that the LLM provider call is about to be issued.

        Call this immediately before the provider request. It selects the
        pessimistic *settle-at-granted* exit over *release* when the lease
        block then raises — once the provider has been contacted, real
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


class WalletClient:
    """gRPC client for the orchestrator-side ``WalletService``.

    Construct over a shared ``grpc.aio`` channel via :meth:`from_channel`
    (the agent reuses the channel it already opens for ``LogService`` —
    RFC 0023 Open Question §1). The only public surface is :meth:`lease`.
    """

    def __init__(
        self,
        stub: wallet_grpc.WalletServiceStub,
        *,
        acquire_max_attempts: int = 3,
        settle_max_attempts: int = 3,
        backoff_base: float = 0.1,
    ) -> None:
        self._stub = stub
        self._acquire_max_attempts = max(1, acquire_max_attempts)
        self._settle_max_attempts = max(1, settle_max_attempts)
        self._backoff_base = max(0.0, backoff_base)

    @classmethod
    def from_channel(cls, channel: grpc.aio.Channel, **kwargs: object) -> WalletClient:
        """Build a client over an existing ``grpc.aio`` channel."""
        return cls(wallet_grpc.WalletServiceStub(channel), **kwargs)  # type: ignore[arg-type]

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff for retry *attempt* (1-indexed)."""
        return self._backoff_base * 2.0 ** (attempt - 1)

    @asynccontextmanager
    async def lease(
        self,
        *,
        agent_id: str,
        model: str,
        estimated_input_tokens: int,
        estimated_max_output_tokens: int,
        cause: walletpb.Cause.ValueType,
        workflow_id: str = "",
        trace_id: str = "",
    ) -> AsyncIterator[Lease]:
        """Acquire a lease, yield it, and close it on exit.

        Raises :class:`BudgetExceededError` if the wallet denies the lease
        or is unreachable — in either case the ``with`` body never runs.
        """
        request = walletpb.LeaseRequest(
            workflow_id=workflow_id,
            agent_id=agent_id,
            model=model,
            # Clamp defensively — a negative estimate would record a
            # negative provisional charge; the wallet also rejects it.
            estimated_input_tokens=max(0, int(estimated_input_tokens)),
            estimated_max_output_tokens=max(0, int(estimated_max_output_tokens)),
            cause=cause,
            trace_id=trace_id,
        )
        lease = await self._acquire(request)
        try:
            yield lease
        except BaseException:
            # Lease cleanup is best-effort and must never mask the exception
            # the caller raised — the agent needs that to tell a budget
            # rejection apart from a provider outage. _release / _settle
            # already swallow transient gRPC failures; this guard catches an
            # *unexpected* error in the cleanup path itself, which would
            # otherwise replace the original exception (RFC 0023 § F). A
            # swallowed cleanup error leaves the lease for the reaper to
            # reconcile at TTL expiry. CancelledError (BaseException, not
            # Exception) is intentionally left to propagate.
            try:
                if not lease._call_started:
                    # The provider was never contacted — fully reverse the hold.
                    await self._release(lease.lease_id, "aborted")
                elif not lease._settled:
                    # The call may have spent real budget — close pessimistically.
                    await self._settle(
                        lease.lease_id,
                        lease.granted_input_tokens,
                        lease.granted_output_tokens,
                        kind="settle-at-granted",
                    )
            except Exception:
                logger.warning(
                    "wallet: lease %s cleanup failed on the exception exit "
                    "path — the reaper will reconcile it at TTL expiry",
                    lease.lease_id, exc_info=True,
                )
            raise
        else:
            if not lease._settled:
                # Clean exit with no explicit settle — defensive close.
                await self._settle(
                    lease.lease_id,
                    lease.granted_input_tokens,
                    lease.granted_output_tokens,
                    kind="settle-at-granted",
                )

    # ── Internals ────────────────────────────────────────────────────

    async def _acquire(self, request: walletpb.LeaseRequest) -> Lease:
        """Acquire a lease, applying the RFC 0023 § F status branching."""
        for attempt in range(1, self._acquire_max_attempts + 1):
            try:
                response = await self._stub.AcquireLease(request)
            except grpc.aio.AioRpcError as exc:
                code = exc.code()
                if code in _UNREACHABLE_CODES:
                    # Fail closed — an agent that cannot reach the wallet
                    # cannot prove it has budget (RFC 0023 § F).
                    raise BudgetExceededError(
                        "wallet unreachable — LLM call failing closed",
                        reason="wallet_unreachable",
                    ) from exc
                if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    # Per-agent active-lease cap — transient; a slot frees
                    # as sibling leases settle. Retry, then surface raw.
                    logger.warning(
                        "wallet: AcquireLease hit the active-lease cap "
                        "(attempt %d/%d)", attempt, self._acquire_max_attempts,
                    )
                    if attempt < self._acquire_max_attempts:
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    raise
                # INTERNAL / INVALID_ARGUMENT / anything else — a server-
                # or agent-side bug. Fail loudly, immediately, no retry.
                logger.error("wallet: AcquireLease failed with %s", code)
                raise

            outcome = response.WhichOneof("outcome")
            if outcome == "denied":
                d = response.denied
                logger.warning(
                    "wallet: lease denied — %s budget exceeded "
                    "(spent=$%.4f limit=$%.4f)",
                    d.scope, d.spent_usd, d.limit_usd,
                )
                raise BudgetExceededError(
                    d.message or f"{d.scope} budget exceeded",
                    scope=d.scope,
                    spent_usd=d.spent_usd,
                    limit_usd=d.limit_usd,
                    estimated_usd=d.estimated_usd,
                    reason="budget_exceeded",
                )
            grant = response.grant
            return Lease(
                self,
                grant.lease_id,
                grant.granted_input_tokens,
                grant.granted_output_tokens,
                grant.ttl_seconds,
            )
        # Unreachable: the loop either returns a Lease or raises.
        raise RuntimeError("wallet: AcquireLease retry loop exited unexpectedly")

    async def _settle(
        self,
        lease_id: str,
        input_tokens: int,
        output_tokens: int,
        *,
        kind: str = "settle",
    ) -> bool:
        """Settle *lease_id*, retrying transient failures with backoff.

        Returns ``True`` on a delivered settlement. A failure that
        outlasts every retry is logged and swallowed (``False``) — the
        orchestrator reaper reconciles the lease at the granted amount,
        so a dropped settlement over-accounts but never loses spend, and
        a settle must never propagate into a successful LLM response."""
        request = walletpb.SettlementRequest(
            lease_id=lease_id,
            actual_input_tokens=max(0, int(input_tokens)),
            actual_output_tokens=max(0, int(output_tokens)),
        )
        for attempt in range(1, self._settle_max_attempts + 1):
            try:
                ack = await self._stub.SettleLease(request)
            except grpc.aio.AioRpcError as exc:
                if attempt < self._settle_max_attempts:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                logger.warning(
                    "wallet: %s RPC failed after %d attempts (%s) — "
                    "the reaper will reconcile lease %s at the granted amount",
                    kind, self._settle_max_attempts, exc.code(), lease_id,
                )
                return False
            if not ack.success:
                logger.warning(
                    "wallet: %s of lease %s rejected: %s",
                    kind, lease_id, ack.error_message,
                )
            return True
        return False

    async def _release(self, lease_id: str, reason: str) -> bool:
        """Release *lease_id* (settle with zero actuals), reversing the hold.

        Single-shot — a failed release leaves the provisional charge for
        the reaper to reconcile at the granted amount on TTL expiry."""
        request = walletpb.ReleaseRequest(lease_id=lease_id, reason=reason)
        try:
            ack = await self._stub.ReleaseLease(request)
        except grpc.aio.AioRpcError as exc:
            logger.warning(
                "wallet: ReleaseLease for %s failed (%s) — the reaper "
                "will reconcile it at TTL expiry", lease_id, exc.code(),
            )
            return False
        if not ack.success:
            logger.warning(
                "wallet: release of lease %s rejected: %s",
                lease_id, ack.error_message,
            )
        return bool(ack.success)
