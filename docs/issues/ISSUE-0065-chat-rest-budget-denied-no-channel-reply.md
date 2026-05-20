---
id: ISSUE-0065
summary: "REST chat endpoint times out with HTTP 504 instead of returning HTTP 200 + reply_status=\"error\" when the wallet denies the lease. RFC 0023 PR 4 wired BudgetExceededError → reply_status=\"error\" on the gRPC SendChatMessage servicer, but the production REST chat path routes via ChannelRouter.PublishAndAwait → agent's ReceiveChannelMessage._dispatch_channel_event, which catches BudgetExceededError with a log line only — no error-reply is published on the channel, so the orchestrator's reply waiter times out."
status: resolved
severity: high
area: agents/persona_runtime
created: 2026-05-20
closed: 2026-05-20
refs:
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/rfcs/0023-pr-plan.md
  - docs/manual-tests/MT-COST-003.md
  - docs/manual-tests/v0.3.2-execution-report.md
  - agents/server_servicers.py
  - agents/channel_publisher.py
  - agents/action_executor.py
  - internal/server/chat_handler.go
---

## Resolution (2026-05-20) — Path A landed

Fixed via Path A from the proposal below:

1. `agents/server_servicers.py::_dispatch_channel_event` now catches
   `BudgetExceededError` ahead of the generic `except Exception` arm and
   publishes a structured-error reply on `event.channel_id` via a new
   helper `_publish_chat_error_on_channel`. The reply carries
   `sender_id=target_agent_id` (wakes the orchestrator's
   `replyWaiter`), `content=exc.message`, and `metadata={"reply_status":
   "error", "error_reason": exc.reason}` as the REST-handler
   discriminator. Falls back to log-only when no channel publisher is
   wired (test fixtures, session-less dispatch).
2. `agents/channel_publisher.py::HTTPChannelPublisher.publish` (and the
   `ChannelPublisher` Protocol) gained an optional `metadata:
   dict[str, Any] | None` kwarg that merges into the wire payload's
   metadata map alongside the reserved `cascade_depth` seat.
3. `agents/action_executor.py::ActionExecutor` gained a public
   `channel_publisher` property so the servicer can reach the publisher
   without poking at the private `_channel_publisher`.
4. `internal/server/chat_handler.go::handleChat` now reads
   `reply.Metadata["reply_status"]` and surfaces `"error"` in the JSON
   envelope; default remains `"ok"`.

Tests pinning the contract:

- Python — `agents/tests/test_chat_path_budget_denial.py`
  `TestDispatchChannelEventBudgetDenial` (5 cases covering budget
  denial, wallet-unreachable, generic-exception negative, happy-path
  negative, no-publisher fallback).
- Python — `tests/unit/python/test_channel_publisher_cascade_depth.py`
  `TestCustomMetadataPassThrough` (2 cases pinning the wire metadata
  pass-through).
- Go — `internal/server/chat_handler_test.go`
  `TestHandleChat_ReplyMetadataReplyStatusErrorSurfacedAs200` plus the
  default-ok regression guard
  `TestHandleChat_ReplyMetadataUnsetDefaultsToReplyStatusOK`.

MT-COST-003 re-run is still required against a built artifact carrying
the fix; the unit tests pin the contract but a live wallet-cap
exhaustion against the orchestrator binary is the canonical
acceptance.

---

## Summary

Under wallet budget denial, the REST chat endpoint
`POST /api/v1/agents/{id}/chat` returns **HTTP 504 `DEADLINE_EXCEEDED`**
instead of the MT-COST-003 spec-required **HTTP 200 + `reply_status="error"` +
`LeaseDenied.message`**. The wallet enforcement itself is correct
(`lease denied — budget exceeded` logs cleanly, no provider spend leak); only
the user-facing chat-error envelope on the REST surface is missing because no
reply-on-channel is published when the agent-side dispatch raises
`BudgetExceededError`.

## Context

Found during the v0.3.2 release-prep MT execution (PR 1) — see
[v0.3.2-execution-report.md MT-COST-003](../manual-tests/v0.3.2-execution-report.md)
for the run notes; this is **F-1** in that report.

The dispatch flow under v0.3.2:

```
Client → POST /api/v1/agents/ember-owl/chat                 (REST)
       → internal/server/chat_handler.go:302               (PublishAndAwait)
       → ChannelRouter.PublishAndAwait on dm:ember-owl:local
       → agent's AgentServiceServicer.ReceiveChannelMessage  (gRPC)
       → agents/server_servicers.py:454-471                  (_dispatch_channel_event)
       → EventDispatcher.dispatch
       → persona action loop
       → LLMClient.create_message(cause=CAUSE_CHANNEL_MESSAGE)
       → WalletClient.lease(...)
       → WalletService.AcquireLease   ← orchestrator denies (budget exceeded)
       → BudgetExceededError raised
       → … no one publishes a reply on dm:ember-owl:local …
       → PublishAndAwait reply waiter times out
       → REST handler returns HTTP 504 ErrChatTimeout
```

The agent-side dispatch wrapper that drops the exception:

```python
# agents/server_servicers.py:454-471
async def _dispatch_channel_event(
    self, target_agent_id: str, event: AgentEvent,
) -> None:
    """Run dispatch and log any exception (no retry in v0.3.0)."""
    try:
        await self._dispatcher.dispatch(target_agent_id, event)
    except Exception as exc:  # noqa: BLE001 — final boundary; logged with traceback
        logger.exception(
            "ReceiveChannelMessage dispatch failed for agent %s (channel %s): %s",
            target_agent_id, event.channel_id, type(exc).__name__,
        )
```

The structured error envelope used by the **gRPC** `SendChatMessage` servicer
([`agents/chat_reply.py::chat_error_response`](../../agents/chat_reply.py),
called from [`server_servicers.py:270-279`](../../agents/server_servicers.py))
has no equivalent on this channel-receive path — `BudgetExceededError` is
caught by the generic `except Exception` arm with a log line only, and no
reply is published on the originating DM channel.

Compounding the wiring gap: `ChannelMessageEvent` proto
([`proto/task.proto:129-200`](../../proto/task.proto)) has **no `metadata` map**
— it carries only structured fields plus `cascade_depth`. The REST handler
sets `metadata["chat_session_id"]` on the inbound `ChannelMessage`
([`internal/server/chat_handler.go:281-283`](../../internal/server/chat_handler.go)),
but the orchestrator → agent gRPC fanout drops the map entirely. So
the agent's `cause_for_event`
([`agents/persona_runtime/wallet_cause.py:17-53`](../../agents/persona_runtime/wallet_cause.py))
sees no `chat_session_id` and derives `CAUSE_CHANNEL_MESSAGE` rather than
`CAUSE_CHAT`. The wallet still gates correctly (verified live — see
v0.3.2 execution report), but the chat-specific surface contract — *"chat →
`reply_status="error"`"* per RFC 0023 §F — is structurally unreachable from
the REST chat path.

## Impact

Operator-visible surface contract violation under budget pressure.

- **Documented expected behaviour** ([MT-COST-003 Step 2](../manual-tests/MT-COST-003.md#step-2-exhaust-the-budget-mid-conversation)):
  > "HTTP status of the denied turn is 200 (not 500, not 503); `reply_status`
  > equals `"error"`; `reply` text references `budget` or `lease` (not a generic
  > `"Internal error"`)."
- **Observed**: HTTP 504 with `{"error":"agent did not respond in time","code":"DEADLINE_EXCEEDED"}`.

Consequences:

1. **Release gate**: blocks the literal v0.3.2 MT-COST-003 acceptance per the
   v0.3.2 release-prep plan PR 1 acceptance criteria
   ([v0.3.2-release-prep-plan.md §PR 1](../v0.3.2-release-prep-plan.md#pr-1--manual-test-execution-report-v032-surface)
   — "every row Pass / Accepted-with-known-gap / Deprecated; zero Fail").
2. **Dashboard noise**: HTTP 504 conflates a budget-denial signal with
   chat-server failures (timeouts, downstream outages). MT-COST-003's design
   note calls this out explicitly: *"surfacing budget denials as 5xx would
   conflate them with chat-server failures and break dashboard incident
   routing."*
3. **Caller behaviour**: a structured `reply_status="error"` is a 200-with-body
   that a client can branch on without retry. A 504 is widely treated as a
   transient infrastructure error → callers retry → wallet denies again →
   user-visible retry storm against a structurally-failing surface.
4. **What still works** (so the v0.3.2 wallet *itself* is not in question):
   - lease lifecycle (granted → settled → denied) logs cleanly
   - no provider spend leak on denied turns (`cost/summary` reflects only
     settled leases)
   - subsequent turns continue to be denied (the wallet does not silently
     recover) — though via 504, not the structured error
   - the gRPC `SendChatMessage` servicer path *does* carry the correct
     structured-error wiring (integration test
     [`tests/integration/test_chat_budget_exhaustion.py`](../../tests/integration/test_chat_budget_exhaustion.py)
     passes) — it is just not the production runtime path; the orchestrator
     REST handler uses `PublishAndAwait` instead.

## Proposed fix / investigation path

Two viable shapes, neither in scope for the v0.3.2 release-prep PR 1 (which
records the finding only):

### Path A — Catch `BudgetExceededError` in `_dispatch_channel_event`, publish a structured-error reply on the originating channel

The lowest-risk fix. Narrow `_dispatch_channel_event`'s exception handling to
also catch `BudgetExceededError` specifically and publish a reply on
`event.channel_id` whose envelope carries the chat-error shape. The
orchestrator's `PublishAndAwait` waiter sees that reply, returns it to the
REST chat handler, which converts it to the `reply_status="error"` JSON body.

Sketch:

```python
# agents/server_servicers.py — _dispatch_channel_event
async def _dispatch_channel_event(
    self, target_agent_id: str, event: AgentEvent,
) -> None:
    try:
        await self._dispatcher.dispatch(target_agent_id, event)
    except BudgetExceededError as exc:
        logger.warning(
            "ReceiveChannelMessage budget-denied for agent %s (channel %s): %s",
            target_agent_id, event.channel_id, exc.message,
        )
        await self._publish_chat_error_on_channel(
            agent_id=target_agent_id,
            channel_id=event.channel_id,
            reply=exc.message,
        )
    except Exception as exc:  # unchanged generic boundary
        logger.exception(
            "ReceiveChannelMessage dispatch failed for agent %s (channel %s): %s",
            target_agent_id, event.channel_id, type(exc).__name__,
        )
```

Requires a small helper that constructs an inbound-shaped channel message
back on `event.channel_id` (the agent already publishes regular replies on
channels via `HTTPChannelPublisher`; this path reuses that surface with a
known-error body shape). The orchestrator's reply correlation primitive
keys on `(channelID, awaitFromAgentID)`, so a normal-shape publish from the
target agent on the same DM channel will resolve the waiter.

Caveat: the published reply needs a discriminator the chat REST handler can
read to set `reply_status="error"` instead of `"ok"` — either a metadata
field on the channel message, or a sentinel marker the REST handler
recognises. Both are small additions; the chat REST handler already inspects
the reply when synthesising the JSON envelope.

### Path B — Propagate `chat_session_id` through `ChannelMessageEvent` and route chat-origin events through the gRPC `SendChatMessage` envelope

A more structural fix. Add a `chat_session_id` field (or a small metadata map)
to the `ChannelMessageEvent` proto so the agent-side `cause_for_event` can
derive `CAUSE_CHAT` correctly, then refactor the chat REST path to invoke
the gRPC `SendChatMessage` servicer (which already carries the
`BudgetExceededError → chat_error_response` wiring) instead of
`PublishAndAwait`. Larger blast radius — touches the proto wire,
`ChannelRouter.fanout`, the chat REST handler, and any cross-process callers
— but eliminates the structural asymmetry that this issue exposes.

Path A is the v0.3.3-or-back-port fix; Path B is the v0.4.0 cleanup that
removes the legacy `chatExecutor` per [ISSUE-0035](ISSUE-0035-chat-executor-dead-but-wired-cleanup.md).

### Tests required (either path)

- **Unit**: a new test in
  `agents/tests/test_chat_path_budget_denial.py` (existing file — currently
  only exercises the gRPC servicer arm) that drives the `_dispatch_channel_event`
  → published-reply path under `BudgetExceededError`, asserting the published
  envelope is the error shape and not silently dropped.
- **Integration**: extend
  [`tests/integration/test_chat_budget_exhaustion.py`](../../tests/integration/test_chat_budget_exhaustion.py)
  with a sibling case that drives the **REST → publish-and-await** path end to
  end, asserting HTTP 200 + `reply_status="error"` + denial-bearing `reply`.
  The existing test exercises the gRPC `SendChatMessage` path only.
- **Manual**: re-run [MT-COST-003](../manual-tests/MT-COST-003.md) on a build
  carrying the fix.

## Notes

> 2026-05-20 — initial capture during v0.3.2 MT execution
> ([v0.3.2-execution-report.md F-1](../manual-tests/v0.3.2-execution-report.md#follow-ups)).
> Wallet enforcement itself is fully working — every lease lifecycle stage
> verified live with cap `per_agent=$0.10`: lease granted (CAUSE_CHANNEL_MESSAGE,
> estimated_usd=$0.094) → LLM call → reconciled (actual_usd=$0.009) → settled.
> Third chat (would push spent $0.017 + estimated $0.084 = $0.101 over the
> $0.10 cap) cleanly denied at the wallet, and the agent raised
> `BudgetExceededError('per_agent budget exceeded: spent=0.017259, limit=0.100000, estimated=0.084555')`.
> But the agent's `_dispatch_channel_event` caught it via the generic
> `except Exception` arm with a log line only, and the orchestrator's
> `PublishAndAwait` waiter timed out → HTTP 504. Cost summary correctly
> showed no provider spend trace for the denied turn — the budget gate fires
> *before* the provider is contacted, so the structural enforcement is intact.
> The miss is purely the operator-visible error envelope on the REST surface.
