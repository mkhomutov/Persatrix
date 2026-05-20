---
id: ISSUE-0066
summary: "REST chat endpoint returns HTTP 504 DEADLINE_EXCEEDED when the wallet's per-agent active-lease cap (gRPC RESOURCE_EXHAUSTED) trips, or when the orchestrator's gRPC rate-limit interceptor denies wallet calls. Same operator-visible surface bug as ISSUE-0065 fixed for BudgetExceededError, but for a different error class: AioRpcError(RESOURCE_EXHAUSTED) falls through _dispatch_channel_event's generic except Exception arm with a log line only, so the REST reply waiter times out instead of returning HTTP 200 + reply_status=\"error\"."
status: resolved
severity: medium
area: agents/persona_runtime
created: 2026-05-20
closed: 2026-05-20
refs:
  - docs/issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/manual-tests/MT-COST-003.md
  - agents/server_servicers.py
  - agents/wallet_client.py
  - internal/wallet/wallet.go
  - internal/security/middleware.go
---

## Resolution (2026-05-20) — Path A landed

Fixed via Path A from the proposal below — extend the existing
published-error arm in `_dispatch_channel_event` with a gated
`grpc.aio.AioRpcError` handler ahead of the generic `except Exception`:

1. `agents/server_servicers.py::_dispatch_channel_event` now nests its
   error handling.  The inner `try` models the two error classes that
   must publish a structured-error reply on the originating channel
   (`BudgetExceededError` from ISSUE-0065; `AioRpcError` with
   `code == RESOURCE_EXHAUSTED` from this issue).  Any other gRPC code
   re-raises from the inner arm and falls through to the outer generic
   `except Exception` logger — silently turning every gRPC error into a
   fake chat reply would mask genuine agent bugs.
2. The `RESOURCE_EXHAUSTED` arm publishes via the same
   `publish_chat_error_on_channel` helper used by the budget-denial arm,
   so the Go-side discriminator (`metadata["reply_status"]="error"`,
   handled in `internal/server/chat_handler.go::handleChat`) needs no
   change.  Distinct `error_reason="resource_exhausted"` keeps the value
   separable from `budget_exceeded` / `wallet_unreachable` on operator
   dashboards.
3. The user-facing reply text is a fixed, friendly retry message
   (`"Agent is at capacity — please retry in a moment."`) rather than
   `exc.details()` — the lease-cap message
   (`"agent already holds the maximum 3 active leases"`) and the
   rate-limit interceptor message (`"rate limit exceeded"`) both leak
   internal-mechanics jargon the end user does not need.

Tests pinning the contract:

- `agents/tests/test_chat_path_resource_exhausted.py`
  `TestDispatchChannelEventResourceExhausted` — 5 cases:
  publish-on-channel, distinct `error_reason`, user-facing reply text
  (no raw `exc.details()` leak), other gRPC codes fall through to the
  generic arm, no-publisher fallback.
- Existing ISSUE-0065 regression guards in
  `agents/tests/test_chat_path_budget_denial.py` still green (12 tests
  total across the two files).

MT-COST-003 re-run is still required against a built artifact carrying
the fix to literally exercise the lease-cap or rate-limit interceptor
path end-to-end; the unit tests pin the contract at
`_dispatch_channel_event` but a live cap saturation against the
orchestrator binary is the canonical acceptance.

---

## Summary

Under the per-agent active-lease cap (RFC 0023 Security Considerations,
a DoS ceiling distinct from a budget denial) — and under the
orchestrator's gRPC rate-limit interceptor — the agent's
`WalletClient.lease()` exhausts its retry budget and re-raises a raw
`grpc.aio.AioRpcError` with `code == RESOURCE_EXHAUSTED`. The exception
propagates up through the persona action loop into
`agents/server_servicers.py::_dispatch_channel_event`, where it is caught
by the **generic `except Exception` arm** with a log line only — no reply
is published on the originating channel, so the orchestrator's REST chat
`PublishAndAwait` reply waiter times out and the caller sees
**HTTP 504 `DEADLINE_EXCEEDED`** instead of MT-COST-003's contracted
**HTTP 200 + `reply_status="error"`**.

This is the *same* surface bug PR #395 fixed for `BudgetExceededError`,
but for a different error class. PR #395 narrowed the new published-error
arm to `BudgetExceededError` deliberately (silently turning every dispatch
crash into a fake chat response would mask agent bugs — see the
`test_generic_exception_does_not_publish_error_reply` regression guard
in [agents/tests/test_chat_path_budget_denial.py](../../agents/tests/test_chat_path_budget_denial.py)),
so a separate, explicit follow-up is required for `RESOURCE_EXHAUSTED`.

## Context

Observed live during the [MT-COST-003 PASS run on 2026-05-20](../manual-tests/MT-COST-003.md#execution-log)
against PR #395 commit `6d17d7c`:

> One intermediate turn returned HTTP 504 from an unrelated
> `AioRpcError(RESOURCE_EXHAUSTED)` (wallet-gRPC active-lease-cap /
> rate-limit interceptor — not `BudgetExceededError`, falls through the
> dispatcher's generic-exception arm); out of scope for ISSUE-0065,
> tracked separately if reproducible.

The two server-side sources both surface the same gRPC status code:

1. **Per-agent active-lease cap** — [`internal/wallet/wallet.go:206-214`](../../internal/wallet/wallet.go)
   denies `AcquireLease` with `status.Errorf(codes.ResourceExhausted, …)`
   when an agent already holds `MaxActiveLeases` concurrent leases. This
   is a self-DoS guard, not a budget denial.

2. **Wallet gRPC rate-limit interceptor** — [`internal/security/middleware.go:172`](../../internal/security/middleware.go)
   denies with `status.Error(codes.ResourceExhausted, "rate limit exceeded")`
   when the global gRPC limiter is saturated.

The agent-side client at [`agents/wallet_client.py:326-336`](../../agents/wallet_client.py)
retries with full-jitter backoff and, on exhausting `acquire_max_attempts`,
re-raises the raw `AioRpcError`. The persona runtime does not translate
this into the structured `BudgetExceededError` envelope (correctly — these
are transient infra signals, not budget violations) and the exception
travels untouched into `_dispatch_channel_event`'s generic arm:

```python
# agents/server_servicers.py — post PR #395
try:
    await self._dispatcher.dispatch(target_agent_id, event)
except BudgetExceededError as exc:
    # … publish structured-error reply on channel (ISSUE-0065 fix) …
except Exception as exc:  # noqa: BLE001 — final boundary
    logger.exception(
        "ReceiveChannelMessage dispatch failed for agent %s (channel %s): %s",
        target_agent_id, event.channel_id, type(exc).__name__,
    )
    # ← AioRpcError(RESOURCE_EXHAUSTED) lands here; no channel reply
```

## Impact

Operator-visible surface contract violation under lease-cap pressure or
gRPC rate-limit saturation.

- Same dashboard-routing problem as ISSUE-0065 — 504 conflates a wallet
  back-pressure signal with chat-server failures (timeouts, downstream
  outages).
- Same client retry-storm risk — 504 is widely treated as a transient
  infra error → clients retry → lease cap stays full → user-visible
  retry storm against a structurally-failing surface (worse than the
  budget case, because the lease cap is itself a back-pressure mechanism
  the retry storm fights against).
- Severity downgraded from ISSUE-0065's `high` to `medium`: budget
  denial is a routine end-of-conversation surface; the active-lease
  cap and rate-limit interceptor only trip under saturation /
  abusive-client patterns, not during a normal interactive session.

## Proposed fix / investigation path

Two viable shapes, both narrower than PR #395's Path A:

### Option A — Extend the existing published-error arm

Add a `grpc.aio.AioRpcError` arm to `_dispatch_channel_event` ahead of
the generic `except Exception`, predicated on `exc.code() ==
grpc.StatusCode.RESOURCE_EXHAUSTED`. Publish through the same
`publish_chat_error_on_channel` helper with a distinct `error_reason`
(e.g. `"lease_cap"` or `"rate_limited"` derived from the status detail)
and an operator-friendly `reply` body. Other gRPC codes continue to
fall through to the generic arm — silently turning every gRPC error
into a fake chat reply would mask the very bugs the generic arm is
there to surface.

Sketch:

```python
except BudgetExceededError as exc:
    # … existing ISSUE-0065 arm …
except grpc.aio.AioRpcError as exc:
    if exc.code() != grpc.StatusCode.RESOURCE_EXHAUSTED:
        raise  # re-fall to generic arm
    logger.warning(
        "ReceiveChannelMessage lease-cap/rate-limit for agent %s "
        "(channel %s): %s",
        target_agent_id, event.channel_id, exc.details(),
    )
    await _publish_chat_error_on_channel(
        self._dispatcher.executor.channel_publisher,
        agent_id=target_agent_id,
        channel_id=event.channel_id,
        inbound_sender_id=event.sender_id,
        reply=(
            "Agent is at capacity — please retry in a moment."
        ),
        reason="resource_exhausted",
    )
except Exception as exc:  # noqa: BLE001 — final boundary
    # … unchanged …
```

This piggybacks on the existing wire (`metadata["reply_status"]="error"`
discriminator, already honoured by [`internal/server/chat_handler.go::handleChat`](../../internal/server/chat_handler.go)),
so the Go side needs no change.

### Option B — Convert at the wallet-client boundary

Have `WalletClient.lease()` translate `RESOURCE_EXHAUSTED` into a
purpose-built exception (`WalletBackpressureError`?) at the
agent-Python boundary, and route it through `_dispatch_channel_event`
alongside `BudgetExceededError`. Cleaner separation of concerns
(servicer never reaches into gRPC status codes) but a larger surface
change for a transient-error case.

**Recommendation**: Option A — the surface gap is at the dispatcher
level (no channel reply on a known back-pressure signal), and PR #395
already established the shape of "catch a specific exception class
ahead of the generic arm and publish through `publish_chat_error_on_channel`".
Option B is the cleanest long-term but does not need to gate the
v0.3.2 / v0.4.0 release surface.

## Notes

> 2026-05-20 — captured from MT-COST-003 PASS run during PR #395 review
> follow-up; one intermediate turn surfaced this code path. Not
> reliably reproducible from the MT-COST-003 fixture (it requires
> hitting either `MaxActiveLeases` concurrency or saturating the
> wallet-gRPC rate-limit interceptor), so a deterministic unit test
> on `_dispatch_channel_event` (mocking `dispatch` to raise
> `AioRpcError(RESOURCE_EXHAUSTED)`) is the right pinning shape, not
> a live MT.
