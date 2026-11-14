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
//     an operator releases it. A quarantine refuses only the ID it names,
//     never calls that send no agent ID (ISSUE-0152). The hooks in
//     middleware.go apply them to every REST API call except /healthz,
//     the login routes and the web console's own routes, and to every
//     call on the agent-facing gRPC server that sends one request and
//     gets one reply. The login routes use two RateLimiters of their own.
//   - [GRPCRecoveryInterceptor] and [GRPCStreamRecoveryInterceptor]: stop a
//     panic in a gRPC handler from crashing the orchestrator.
//   - [AgentEnrollment], made by [NewAgentEnrollment]: registers an agent
//     and issues its token as one step, and deregisters it and revokes its
//     tokens as one step (RFC 0009 Phase 4). The REST server's registration
//     and deregistration routes go through it under either `auth.mode`.
//   - [AgentTokenIssuer], made by [NewAgentTokenIssuer]: issues each agent a
//     signed token that names it, checks a token and revokes an agent's
//     tokens (RFC 0009 Phase 4). Registration answers with a token under
//     either mode. Tokens are checked in one place only: on REST calls
//     under `auth.mode: enabled`. There a call that shows a token is
//     refused unless the token is good and names the same agent as the
//     X-Agent-ID beside it, when the call sends one. Registration, which
//     is how an agent gets a token, checks none.
//
// The rate limiter has gaps. Agent IDs are self-reported on gRPC, and on
// REST under `auth.mode: disabled`, so a caller can invent one to get a
// fresh budget, or send a real agent's ID on gRPC to get that agent
// quarantined (ISSUE-0152); on REST under `enabled` only an agent token
// names an agent ([WithAgentID]). Callers that send no agent
// ID share one bucket: today the CLI and any other client that sends
// none, since the agents send theirs on REST and wallet calls; under
// `enabled` a signed-in caller gets a budget of its own ([WithCallerKey]).
// An agent's REST calls spend a budget apart from its wallet calls', and a
// REST refusal counts toward no quarantine (ISSUE-0187). The stream that
// carries agents' logs to the orchestrator skips both checks.
// And a quarantine does not stop the orchestrator sending the agent tasks,
// though RFC 0009 says it should.
//
// Agent tokens have gaps too. `auth.mode` is `disabled` as shipped, and
// then no token is checked. Under `enabled` the REST server goes by the
// agent a token proves for the rate limit, the quarantine and the sender
// of a channel message, but the routes the agents use need no credential:
// a caller that shows none can read channel lists and histories, and
// register or deregister an agent. Only a publish is bound to its caller:
// its sender must be the agent a token proves, or the signed-in account's
// own participant. No gRPC call checks a token under either mode. And a
// token proves less than it should: registration admits a caller that
// shows no fleet enrollment secret even when one is set (only a wrong
// secret is refused), and deregistration is open, so anyone who can reach
// the server can register under an agent's ID and be given a token for it.
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
// Planned for agent identity (RFC 0009 Phase 4), all under `enabled`:
// closing the agents' routes to a caller with no credential, requiring the
// enrollment secret at registration, binding deregistration to the agent
// itself or an operator, and checking tokens on the wallet's gRPC calls.
// Until then an agent's ID is proved only where the list above says a
// token is checked, and is taken on trust everywhere else. ROADMAP.md has
// the target release.
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
