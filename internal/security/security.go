// Package security implements permission gates, rate limiting, audit logging,
// and external input sanitization for the Persatrix orchestrator.
//
// RFC 0009 §G + §I components shipped in this package:
//
//   - [AuditLogger] (interface) and [NewFileAuditLogger] (JSONL append-only
//     sink with checksum-chained tamper evidence and severity-driven flush).
//   - [Redactor] (interface) and [NewSecretRedactor] (5 default patterns +
//     reflective struct walk with cycle/depth bounds).
//   - [AuditEvent] / [AuditEventType] (closed enum, severity-classified).
//
// Phase 1b (rate limiting) and Phase 2 (input sanitisation, ContextItem,
// provenance tagging) land in the follow-up RFC 0009 PRs.
package security

// TODO: Implement PermissionGate (deny-by-default, glob path matching) — RFC 0009 Phase 1
// (PR 233 review SF-6: PermissionGate is a Phase 1 component per the RFC and the
// PR plan; the previous "Phase 3" tag conflated it with identity tokens / HITL).
// RateLimiter + CircuitBreaker + REST/gRPC middleware shipped in PR 2 — see
// ratelimit.go, circuitbreaker.go, middleware.go.
// TODO: Implement InputSanitizer (bridge/A2A/webhook input wrapping + filtering) — RFC 0009 PR 3.
