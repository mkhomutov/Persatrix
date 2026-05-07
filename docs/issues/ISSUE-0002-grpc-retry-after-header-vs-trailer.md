---
id: ISSUE-0002
summary: "GRPCRateLimitInterceptor godoc says trailer but uses grpc.SetHeader; align doc + add client-side test"
status: resolved
severity: medium
area: security
created: 2026-05-04
closed: 2026-05-07
refs:
  - docs/rfcs/0009-security-sandboxing.md
---

## Summary

`GRPCRateLimitInterceptor` documents that it "sets a `retry-after-seconds`
gRPC trailer" but the implementation calls `grpc.SetHeader`, which writes
initial metadata, not trailers (`grpc.SetTrailer`).

## Context

- File: [internal/security/middleware.go](../../internal/security/middleware.go) → `GRPCRateLimitInterceptor`.
- For a unary RPC returning an error, headers are flushed alongside the
  status, so clients can read the value today — but the godoc / code mismatch
  is a contract bug that will mislead the next gRPC-surface auditor.
- `SetHeader`'s error return is intentionally ignored.

## Impact

Contract / documentation accuracy. No runtime breakage, but anyone reading
the godoc will reach for the wrong client API (`Trailer()` vs `Header()`)
when implementing retry handling.

## Proposed fix / investigation path

Pick one and align both the implementation and the godoc:

- **Option A (preferred — parity with HTTP `Retry-After`):** keep
  `grpc.SetHeader`, rewrite the godoc to say "header metadata".
- **Option B:** switch to `grpc.SetTrailer`, keep the trailer wording.

Then add a client-side test using a `grpc.UnaryClientInterceptor` (or the
`grpc.Header(&md)` / `grpc.Trailer(&md)` `CallOption`s) that asserts the
metadata is present on `ResourceExhausted` responses.

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding M-R3-03).
