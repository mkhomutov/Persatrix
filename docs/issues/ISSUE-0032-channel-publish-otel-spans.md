---
id: ISSUE-0032
summary: "Add OpenTelemetry spans around GRPCMessageDispatcher.Dispatch and HTTPChannelPublisher.publish for trace navigation"
status: resolved
severity: low
area: internal/observability
created: 2026-05-05
closed: 2026-05-08
refs:
  - internal/channels/grpc_dispatcher.go
  - agents/channel_publisher.py
  - docs/observability.md
---

## Summary

The new cross-process publish path (Python REST publisher → orchestrator
fanout → Go gRPC dispatcher → recipient `ReceiveChannelMessage`) relies
solely on aiohttp / gRPC autoinstrumentation spans. There is no
business-logic span carrying the fields an operator actually pivots
on: `channel_id`, `recipient`, `message_id`.

## Context

Observed during PR #250 review (Nice-to-Have #3). The autoinstrumentation
spans exist and will already correlate the HTTP client → server →
gRPC client → server hops, but the trace UI shows only HTTP method /
RPC name on the parents. Drilling to which channel/recipient was
involved currently requires opening child span attributes.

## Impact

- Slower incident triage for channel delivery failures.
- Harder to build per-channel SLO dashboards from trace data.

## Proposed fix / investigation path

- Python: wrap `HTTPChannelPublisher.publish` in a span named
  `channel.publish` with attributes `channel.id`, `channel.sender_id`,
  `channel.mentions_count`, `channel.message_id`.
- Go: wrap `GRPCMessageDispatcher.Dispatch` in a span named
  `channel.dispatch` with attributes `channel.id`, `recipient.agent_id`,
  `recipient.address`, `channel.message_id`.

Both spans should set their status to error on failure and record the
returned error via `RecordError` / `set_status`.

Coordinate naming with `docs/observability.md` so dashboards can rely
on stable attribute keys.

## Notes

> 2026-05-05 — initial capture during PR #250 review.
>
> 2026-05-08 — Go side resolved. `GRPCMessageDispatcher.Dispatch` now
> emits a `channel.dispatch` span (tracer
> `persatrix/channels/dispatch`) carrying `channel.id`,
> `channel.message_id`, `recipient.agent_id`, and `recipient.address`
> (the last set only after the registry yields a real address — empty
> drops do not pollute address-cardinality dashboards). Every error
> branch fires `RecordError` + `SetStatus(Error)`; the at-most-once
> silent-drop branch (unknown participant) leaves status `Unset` per
> RFC 0011 §C "Delivery guarantees". Pinned by four span tests in
> `internal/channels/grpc_dispatcher_test.go`
> (`TestGRPCMessageDispatcher_HappyPathEmitsChannelDispatchSpan`,
> `…_DegradedAgentSpanRecordsError`,
> `…_RPCStatusErrorRecordedOnSpan`,
> `…_UnknownParticipantSpanIsBenign`) backed by a package-wide
> `tracetest.InMemoryExporter` installed via `TestMain` (per-test
> providers do not work — `otel.SetTracerProvider`'s
> `delegateTracerOnce` guard locks the package-level
> `dispatcherTracer` to the first provider it sees).
>
> Python side (`HTTPChannelPublisher.publish`, `channel.publish` span)
> intentionally not folded into this PR — the Go change is single-file
> and the cross-language work is best sized into its own follow-up so
> the agent runtime gets its own focused review.
>
> 2026-05-08 — Python side resolved. `HTTPChannelPublisher.publish`
> now wraps the entire publish attempt (including the sticky-disabled
> short-circuit) in a `channel.publish` span carrying `channel.id`,
> `channel.sender_id`, `channel.mentions_count`, and (on success)
> `channel.message_id` lifted from the orchestrator's 201 response.
> Status discipline mirrors the Go side: HTTP 503 channels-disabled
> branch records the exception event but leaves status `UNSET`
> (deployment signal, not an internal failure — flagging it ERROR
> would inflate error-rate dashboards on every channels-off run);
> other 4xx/5xx and transport failures `RecordException` +
> `Status(ERROR)`. Pinned by 8 span tests in
> `tests/unit/python/test_channel_publish_otel.py` and an updated
> §10.2 row + §10.1 cross-process exception note in
> `docs/observability.md`.
