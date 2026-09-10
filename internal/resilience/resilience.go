// Package resilience is a placeholder for planned failure handling:
// retrying failed model calls, falling back to another model, and a
// dead-letter queue that keeps tasks that failed for good so an operator
// can inspect them. Nothing is implemented yet: the TODOs below are the
// plan, sketched in docs/ai-agents-orchestration-spec.md §6.7 (the circuit
// breaker there works per agent; this package's would work per model
// provider, as the note below explains). ROADMAP.md tracks the status.
//
// Some retrying already happens elsewhere. The Anthropic and OpenAI client
// libraries retry a model call that hits a rate limit, a timeout or a
// server error, and agents/llm_providers.py keeps their default retry
// count. A loop in internal/executor retries sending a task to an agent
// when the send itself fails, but not a task the agent reports as failed.
//
// Note on CircuitBreaker (PR #244 round-2 review M-03): the
// per-agent, configurable-threshold circuit breaker for the
// **security policy** layer ships as `internal/security.CircuitBreaker`
// (RFC 0009 PR 2). It is wired into the REST + gRPC rate-limit
// middleware and is keyed on the agent ID the caller sends. Only
// rate-limit violations reach it, so it quarantines an agent that keeps
// hitting the rate cap. It also has thresholds for capability,
// tool-denied and flagged-input violations, but no code reports those.
// The resilience-package breaker that remains TODO
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
