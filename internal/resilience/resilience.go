// Package resilience is a placeholder for planned failure handling:
// retrying failed model calls, falling back to another model, and a
// dead-letter queue that keeps tasks that failed for good so an operator
// can inspect them. Nothing is implemented yet: the TODOs below are the
// plan, and ROADMAP.md lists the package as a TODO stub with no target
// release. Sending a task to an agent is already retried, by a loop inside
// internal/executor.
//
// Note on CircuitBreaker (PR #244 round-2 review M-03): the
// per-agent, configurable-threshold circuit breaker for the
// **security policy** layer (capability / rate-limit / tool-denied
// quarantine) ships as `internal/security.CircuitBreaker` (RFC 0009
// PR 2). It is wired into the REST + gRPC middleware and is keyed on
// agent identity. The resilience-package breaker that remains TODO
// below is for a different concern: **model-call failover** — short-
// circuiting requests to a misbehaving LLM provider so the
// orchestrator falls back to a healthy one instead of hammering the
// failing endpoint. The two breakers may converge in a future RFC,
// but until then they have distinct keys (agent ID vs. provider
// endpoint), distinct trip conditions (security policy vs.
// success/error rate), and distinct recovery semantics (operator
// unquarantine vs. half-open probing).
package resilience

// TODO: Implement CircuitBreaker (per-model-provider, success/error rate
//       — distinct from internal/security.CircuitBreaker; see package doc)
// TODO: Implement RetryPolicy (exponential backoff, max attempts)
// TODO: Implement FallbackChain (model failover)
// TODO: Implement DeadLetterQueue (store failed tasks for inspection)
