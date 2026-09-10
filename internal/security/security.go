// Package security holds the orchestrator's security controls. RFC 0009
// (Agent Identity, Security & Sandboxing) is the design.
//
// Built and in use:
//
//   - [AuditLogger], made by [NewFileAuditLogger]: an append-only log of
//     security events. Each line's checksum is chained to the line before
//     it, so editing an old line breaks the chain; [VerifyChain] reports
//     the first break.
//   - [Redactor], made by [NewSecretRedactor]: replaces API keys and other
//     secrets with a marker. The audit logger runs every event through it.
//   - [RateLimiter] and [CircuitBreaker]: cap how often each agent may call
//     the orchestrator, and quarantine (block) an agent that keeps hitting
//     the cap. They sit in front of the REST API and the agent-facing gRPC
//     server (middleware.go).
//   - [GRPCRecoveryInterceptor] and [GRPCStreamRecoveryInterceptor]: stop a
//     panic in a gRPC handler from crashing the orchestrator.
//
// Built but not used by the orchestrator yet: [InputSanitizer], which flags
// prompt injection (text that tries to override an agent's instructions,
// change its role or send data out). Its pattern list is the source of the
// Python copy in agents/security_patterns.py (written by cmd/genpatterns),
// which the agents apply to channel messages and to the output of tools
// that bring in outside text. It was also meant for bridge and A2A
// messages, but internal/bridges and internal/a2a are still placeholders.
//
// Planned: agent identity tokens, so the orchestrator can check which agent
// is calling (RFC 0009 Phase 4). Until then, the agent ID a caller sends is
// taken on trust. ROADMAP.md has the target release.
//
// Tool permissions are checked in the Python agents, by the deny-by-default
// PermissionGate in agents/tools/permissions.py. The Go PermissionGate below
// is only a TODO.
package security

// TODO: Implement PermissionGate (deny-by-default, glob path matching).
// RFC 0009 does not plan one: none of its phases lists a Go PermissionGate,
// and it treats the Python gate in agents/tools/permissions.py as the check
// it builds on.
