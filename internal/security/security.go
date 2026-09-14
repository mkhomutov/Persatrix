// Package security holds the orchestrator's security controls. RFC 0009
// (Agent Identity, Security & Sandboxing) is the design.
//
// Built and in use:
//
//   - [AuditLogger], made by [NewFileAuditLogger]: an append-only log of
//     security events. Each line's checksum is chained to the line before
//     it, so editing an old line breaks the chain.
//   - [Redactor], made by [NewSecretRedactor]: replaces API keys and other
//     secrets with a marker. The audit logger runs every event through it.
//   - [RateLimiter] and [CircuitBreaker]: count calls under the agent ID
//     the caller sends, refuse calls over the cap, and quarantine an agent
//     that keeps hitting it. A quarantined agent's calls are refused until
//     an operator releases it, and while any agent is quarantined, calls
//     that send no agent ID are refused too. The hooks in middleware.go
//     apply them to every REST API call except /healthz, the login routes
//     and the web console's own routes, and to every call on the
//     agent-facing gRPC server that sends one request and gets one reply.
//     The login routes use two RateLimiters of their own.
//   - [GRPCRecoveryInterceptor] and [GRPCStreamRecoveryInterceptor]: stop a
//     panic in a gRPC handler from crashing the orchestrator.
//
// The rate limiter has gaps. Callers that send no agent ID share one
// bucket, and today that means the CLI and every REST call from the agents,
// which send their ID only on wallet calls (ISSUE-0111). The stream that
// carries agents' logs to the orchestrator skips both checks. And a
// quarantine does not stop the orchestrator sending the agent tasks, though
// RFC 0009 says it should.
//
// Built but not called by the orchestrator yet:
//
//   - [InputSanitizer], which flags prompt injection (text that tries to
//     override an agent's instructions, change its role or send data out).
//     RFC 0009 wants the orchestrator to run it on outside data as it
//     arrives, such as messages from channel bridges and webhooks, and it
//     was also meant for A2A (the Agent2Agent protocol) input;
//     internal/bridges and internal/a2a are still placeholders.
//     cmd/genpatterns copies its pattern list to
//     agents/security_patterns.py, and the [ContextSource] and
//     [SanitizerAction] values to agents/security_enums.py. The Python
//     agents run that copy on channel messages and on tool output that
//     brings in outside text.
//   - [VerifyChain], which checks an audit log's checksum chain and reports
//     the first break. Only tests call it: neither the orchestrator nor the
//     CLI checks the chain.
//
// Planned: agent identity tokens, so the orchestrator can check which agent
// is calling (RFC 0009 Phase 4). Until then, the agent ID a caller sends is
// taken on trust. ROADMAP.md has the target release.
//
// Tool permissions are checked in the Python agents: PermissionGate in
// agents/tools/permissions.py (deny-by-default) and PathValidator in
// agents/tools/sandbox.py (file paths, matched against glob patterns). This
// package has no permission gate of its own; a TODO in security.go records
// the idea.
package security

// TODO: Implement PermissionGate (deny-by-default, glob path matching).
// RFC 0009 does not plan one: it builds on the Python checks named in the
// package comment. Two later RFCs expect an orchestrator-side gate: RFC 0013
// adds tool risk checks to one, and RFC 0043 cites this TODO as its missing
// capability gate. Settle in those RFCs whether the orchestrator needs its
// own gate before building one.
