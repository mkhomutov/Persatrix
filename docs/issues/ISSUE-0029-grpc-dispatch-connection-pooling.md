---
id: ISSUE-0029
summary: "Per-dispatch gRPC dial in both internal/executor/dispatch.go and internal/channels/grpc_dispatcher.go — consolidate connection management for v0.4.0"
status: open
severity: medium
area: internal/channels
created: 2026-05-05
refs:
  - internal/channels/grpc_dispatcher.go
  - internal/executor/dispatch.go
---

## Summary

`GRPCMessageDispatcher.Dispatch` opens a new gRPC client connection
per call, mirroring the pattern in `internal/executor/dispatch.go`.
Both call sites carry inline TODOs flagging consolidation, but the
follow-up is currently only tracked in code comments.

## Context

Observed during PR #250 review (Medium #2 + Nice-to-Have #4). PR #250
preserved parity with the executor dispatcher to keep the diff
focused; the consolidation belongs to a v0.4.0 phase.

## Impact

- Fan-out scenarios (e.g. 8-member group channel publish) take
  N × dial overhead today.
- Once mTLS lands (RFC 0009 Phase 4), each dial includes a TLS
  handshake — the cost compounds.
- The two call sites can drift in dial-option set (keepalive, retry,
  message size) if they are migrated separately.

## Proposed fix / investigation path

Introduce a shared `internal/agentsdial.Pool` (or extend an existing
helper) that:

- Caches `*grpc.ClientConn` keyed by agent address.
- Honours per-conn idle TTL and a max-pool-size eviction policy.
- Exposes `Dial(ctx, addr) (*grpc.ClientConn, releaseFunc, error)`
  so the dispatcher does not own connection lifetime.
- Is wired from both `internal/executor/dispatch.go` and
  `internal/channels/grpc_dispatcher.go` simultaneously to prevent
  drift.

Defer to v0.4.0; capture the design here before the call-sites diverge.

## Notes

> 2026-05-05 — initial capture during PR #250 review.

> 2026-10-18 — v0.4.0 planning-readiness: **not slotted.** "Defer to v0.4.0"
> above, and "for v0.4.0" in the summary, predate [v0.4.0's
> shape](../v0.4.x-sequencing.md#amendment-2026-10-18--v040s-shape-its-prerequisites-its-cuts-and-the-end-of-the-v03x-line),
> which does not take it in: its [ruling (q)](../v0.4.x-sequencing.md#rulings)
> boards an issue only when a phase in the release is its fix. RFC 0009 Phase
> 4 now boards v0.4.0, but for capability tokens; it does not bring the mTLS
> the Impact section counts on, so the cost argument there is unchanged.
