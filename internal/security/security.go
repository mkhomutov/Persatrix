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

// TODO: Implement PermissionGate (deny-by-default, glob path matching) — RFC 0009 Phase 3.
// TODO: Implement RateLimiter (per-agent action rate limits) — RFC 0009 PR 2.
// TODO: Implement InputSanitizer (bridge/A2A/webhook input wrapping + filtering) — RFC 0009 PR 3.
