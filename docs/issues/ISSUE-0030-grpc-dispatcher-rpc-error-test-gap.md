---
id: ISSUE-0030
summary: "GRPCMessageDispatcher tests do not cover RPC errors (codes.Unavailable / codes.Unimplemented) — only context cancellation"
status: resolved
severity: low
area: internal/channels
created: 2026-05-05
closed: 2026-05-07
closed_pr: 268
refs:
  - internal/channels/grpc_dispatcher.go
  - internal/channels/grpc_dispatcher_test.go
---

## Summary

`grpc_dispatcher_test.go` exercises the dial happy path, status/address
gating, unknown-participant noop, zero-timestamp default, unknown
channel-id-prefix warning, and ctx-cancel-mid-call. It does not cover
the case where `ReceiveChannelMessage` returns a gRPC status error
(`codes.Unavailable`, `codes.Unimplemented`, etc.). The
`recordingAgentServer.respond` helper already supports this; one extra
case would close the gap cheaply.

## Context

Observed during PR #250 review (Nice-to-Have #1). The error path
returns the wrapped `status.Error` to the caller — currently trusted
by code reading.

## Impact

- A regression that swallows `Unavailable` (e.g. accidentally returning
  `nil` on transient RPC errors) would not be caught.
- The contract that `Unimplemented` propagates rather than degrades to
  silent drop is not pinned.

## Proposed fix / investigation path

Add a table case to the existing `TestGRPCMessageDispatcher_Dispatch`
suite that primes `recordingAgentServer.respond` with
`status.Error(codes.Unavailable, "boom")` and asserts:

1. `Dispatch` returns a non-nil error wrapping the gRPC status.
2. `errors.Is`/`status.Code` against the returned error recovers
   `codes.Unavailable`.

## Notes

> 2026-05-05 — initial capture during PR #250 review.
>
> 2026-05-07 — closed. `TestGRPCMessageDispatcher_RPCStatusErrorPropagates`
> added in `internal/channels/grpc_dispatcher_test.go` covers both
> `codes.Unavailable` and `codes.Unimplemented` via a table-driven shape
> using the existing `recordingAgentServer.respond` seam.
