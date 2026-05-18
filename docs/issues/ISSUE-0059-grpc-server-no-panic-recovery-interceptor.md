---
id: ISSUE-0059
summary: The orchestrator's agent-facing gRPC server (LogService + WalletService) registers no panic-recovery interceptor, unlike the HTTP server's recoveryMiddleware — an unrecovered panic in any gRPC handler crashes the whole orchestrator process. RFC 0023 PR 2 grows the wallet handlers' panic surface and adds a reaper goroutine that an interceptor cannot cover.
status: resolved
severity: medium
area: grpc
created: 2026-05-18
closed: 2026-05-18
closed_pr: 379
refs:
  - docs/rfcs/0023-pr-plan.md
  - docs/rfcs/0023-llm-call-leasing.md
  - cmd/orchestrator/main.go
  - cmd/orchestrator/grpcserver.go
  - internal/server/middleware.go
  - internal/security/recovery.go
---

## Summary

The agent-facing gRPC server in `cmd/orchestrator/main.go` — the listener
that hosts `LogService` and, since RFC 0023 PR 1 (#378), `WalletService` —
is configured with a single `grpc.UnaryInterceptor` (the RFC 0009
rate-limiter) and no panic-recovery interceptor. The HTTP server, by
contrast, wraps every handler in `recoveryMiddleware`
(`internal/server/middleware.go`). An unrecovered panic in any gRPC
handler therefore terminates the whole orchestrator process.

## Context

Captured during the RFC 0023 PR 1 (#378) review. PR 1 registers the
always-grant `WalletService` skeleton on the existing
`grpc.NewServer(...)` built inside `main.go`'s `if logBuf != nil` block:

```go
grpcServer = grpc.NewServer(
    grpc.StatsHandler(otelgrpc.NewServerHandler()),
    grpc.MaxRecvMsgSize(8*1024*1024),
    grpc.MaxConcurrentStreams(256),
    grpc.KeepaliveEnforcementPolicy(...),
    grpc.UnaryInterceptor(security.GRPCRateLimitInterceptor(rateLimiter, circuitBreaker)),
)
```

gRPC-go does **not** recover handler panics by default — a panic
propagates out of the per-RPC goroutine and crashes the process. This is
*not* introduced by PR 1: it is a pre-existing gap that affects
`LogService` identically. PR 1 is explicitly "registration only; no
listener change", so hardening the shared listener there would be scope
creep — hence this standalone issue rather than a PR-1 change.

## Impact

- **Today**: latent. The current gRPC handlers (`LogService`, the
  always-grant `WalletService` skeleton) have no known panic path, so the
  process-crash risk is unrealised.
- **RFC 0023 PR 2**: the surface grows. PR 2 composes `BudgetEnforcer`
  into `AcquireLease` under a coarse mutex and adds an in-flight lease map
  — more handler code, more panic potential — and adds the reaper
  goroutine `reapLoop`. A panic in the reaper goroutine is **not** an
  RPC-handler frame, so even a recovery *interceptor* would not catch it;
  the reaper needs its own `defer`/`recover`.

A crash drops every in-flight log stream and, once the wallet is
load-bearing (PRs 3–6), blocks every agent from acquiring a lease until
the orchestrator restarts.

## Proposed fix / investigation path

Two independent pieces:

1. **Recovery interceptor on the gRPC server.** Add a hand-rolled unary
   recovery interceptor mirroring `recoveryMiddleware` — `defer`/`recover`,
   log the panic + `debug.Stack()`, return `status.Error(codes.Internal,
   ...)`. Compose it as the **outermost** interceptor via
   `grpc.ChainUnaryInterceptor(recovery, rateLimit)` so it also catches
   panics raised inside the rate-limit interceptor. A hand-rolled
   interceptor keeps parity with the HTTP precedent and avoids adding
   `go-grpc-middleware` as a new dependency. (A future streaming RPC would
   also need the stream variant — cf. the existing
   `TODO(rfc0009-phase4)` on `grpc.StreamInterceptor`.)

2. **Panic guard on the reaper goroutine.** Independently, RFC 0023 PR 2's
   `reapLoop` must carry its own `defer func() { if r := recover(); ... }()`
   — a server interceptor never wraps a background goroutine. This piece
   is squarely PR 2's responsibility and is cross-referenced from the
   [RFC 0023 PR plan PR 2 section](../rfcs/0023-pr-plan.md).

Piece (2) is folded into RFC 0023 PR 2. Piece (1) is broader orchestrator
hardening (it touches `main.go`'s shared server options, not just the
wallet) — it can ride along with PR 2 or land as a standalone change;
that sequencing is a reviewer judgment call, not a blocker.

## Notes

> 2026-05-18 — initial capture during the RFC 0023 PR 1 (#378) review.
> The gap is pre-existing and not introduced by #378; filed as a
> follow-up because PR 1's scope is registration-only. The reaper-goroutine
> guard (piece 2) is cross-referenced into the RFC 0023 PR 2 plan section.

> 2026-05-18 — resolved by #379. Piece (1) landed: `GRPCRecoveryInterceptor`
> (`internal/security/recovery.go`) is composed as the outermost link of
> `grpc.ChainUnaryInterceptor` on the agent-facing gRPC server, recovering a
> handler panic as `codes.Internal` — parity with the HTTP
> `recoveryMiddleware`. The shared `grpc.NewServer(...)` construction was
> extracted from `main.go` into `newAgentGRPCServer` (`grpcserver.go`) to
> stay within the file-size budget. Piece (2) — the RFC 0023 PR 2 reaper
> goroutine's own `defer`/`recover` — is unaffected by this closure and
> remains a PR 2 checklist item: a server interceptor never wraps a
> background goroutine.
