package security

import "time"

// AuditEventType identifies the kind of security-relevant event being recorded.
//
// The full set is fixed at compile time so the table-driven severity
// classifier in [IsSecurityEvent] can be exhaustively unit-tested via
// [AllAuditEventTypes]. Adding a new constant without also adding a
// classification entry will be caught by
// `TestEveryAuditEventType_HasSeverityClassification`.
type AuditEventType string

// Audit event constants. The set mirrors RFC 0009 §G plus the chain-recovery
// constants required by PR #232 review SF-3 (audit log integrity on restart).
//
// Constants whose wiring lands in later PRs are reserved here intentionally —
// adding them now keeps the severity-classifier table closed and avoids a
// breaking schema change when the corresponding emit sites are added (per the
// per-PR plan note "no procedural-memory hooks" Open Question 8 resolution).
const (
	// Lifecycle
	AuditAgentRegistered     AuditEventType = "agent.registered"
	AuditAgentTokenIssued    AuditEventType = "agent.token_issued"  // reserved (Phase 3 — agent identity tokens)
	AuditAgentTokenInvalid   AuditEventType = "agent.token_invalid" // reserved (Phase 3)
	AuditCapabilityViolation AuditEventType = "capability.violation"

	// Tool dispatch
	AuditToolInvoked     AuditEventType = "tool.invoked"
	AuditToolDenied      AuditEventType = "tool.denied"
	AuditToolArgInvalid  AuditEventType = "tool.arg_invalid"
	AuditToolRateLimited AuditEventType = "tool.rate_limited"

	// Memory operations (reserved — wired with RFC 0008 shared-pool ACL in v0.4.0)
	AuditMemoryRead   AuditEventType = "memory.read"
	AuditMemoryWrite  AuditEventType = "memory.write"
	AuditMemoryDenied AuditEventType = "memory.denied"

	// Input handling (Phase 2 wiring)
	AuditInputFlagged AuditEventType = "input.flagged"

	// Human-in-the-loop (reserved — Phase 4)
	AuditHITLGateOpened AuditEventType = "hitl.gate_opened"
	AuditHITLApproved   AuditEventType = "hitl.approved"
	AuditHITLRejected   AuditEventType = "hitl.rejected"

	// Rate limiting (PR 2)
	AuditRateLimitViolated            AuditEventType = "rate_limit.violated"
	AuditRateLimitUnauthenticatedCall AuditEventType = "rate_limit.unauthenticated_caller"

	// Audit-log lifecycle (chain-recovery — PR #232 review SF-3)
	AuditChainBootstrap AuditEventType = "chain.bootstrap"
	AuditChainRestart   AuditEventType = "chain.restart"
	AuditChainRecovered AuditEventType = "chain.recovered"
)

// AllAuditEventTypes returns every defined [AuditEventType] for use by the
// closed-set classifier test (`TestEveryAuditEventType_HasSeverityClassification`).
//
// Ordering is stable and arbitrary — callers must not depend on it.
func AllAuditEventTypes() []AuditEventType {
	return []AuditEventType{
		AuditAgentRegistered,
		AuditAgentTokenIssued,
		AuditAgentTokenInvalid,
		AuditCapabilityViolation,
		AuditToolInvoked,
		AuditToolDenied,
		AuditToolArgInvalid,
		AuditToolRateLimited,
		AuditMemoryRead,
		AuditMemoryWrite,
		AuditMemoryDenied,
		AuditInputFlagged,
		AuditHITLGateOpened,
		AuditHITLApproved,
		AuditHITLRejected,
		AuditRateLimitViolated,
		AuditRateLimitUnauthenticatedCall,
		AuditChainBootstrap,
		AuditChainRestart,
		AuditChainRecovered,
	}
}

// securityEvents lists event types whose audit records require per-event
// fsync. Membership rationale (RFC 0009 §G + PR 1 plan):
//
//   - Capability and rate-limit violations are the primary signals an
//     attacker is probing the system; losing them on crash defeats the audit.
//   - HITL outcomes carry a human decision that must not silently disappear.
//   - Token-validation outcomes (Phase 3) gate every authenticated call.
//   - Chain-bootstrap and chain-recovered (PR #232 review SF-3) mark integrity
//     boundaries an operator must be able to detect after the fact.
//   - Unauthenticated-caller rate-limit hits (PR #232 review SF-6) are emitted
//     under flooding attack conditions exactly when batched events are most
//     likely to be lost on crash; per-event fsync cost is bounded by the
//     rate limit itself.
//
// Everything else (`tool.invoked`, `memory.read`, etc.) is telemetry-class
// and may be batched.
var securityEvents = map[AuditEventType]struct{}{
	AuditCapabilityViolation:          {},
	AuditAgentTokenIssued:             {},
	AuditAgentTokenInvalid:            {},
	AuditToolDenied:                   {},
	AuditToolRateLimited:              {},
	AuditMemoryDenied:                 {},
	AuditInputFlagged:                 {},
	AuditHITLGateOpened:               {},
	AuditHITLApproved:                 {},
	AuditHITLRejected:                 {},
	AuditRateLimitViolated:            {},
	AuditRateLimitUnauthenticatedCall: {},
	AuditChainBootstrap:               {},
	AuditChainRestart:                 {},
	AuditChainRecovered:               {},
}

// telemetryEvents is the explicit allow-list of batched event types.
// Closed-set: any event type missing from BOTH securityEvents AND this map
// causes `TestEveryAuditEventType_HasSeverityClassification` to fail.
var telemetryEvents = map[AuditEventType]struct{}{
	AuditAgentRegistered: {},
	AuditToolInvoked:     {},
	AuditToolArgInvalid:  {},
	AuditMemoryRead:      {},
	AuditMemoryWrite:     {},
}

// IsSecurityEvent reports whether t requires per-event fsync (vs batched flush).
//
// PR #233 deep-review M-1: previously this returned false for unknown
// types, batching them. The docstring claimed "fails closed on telemetry
// latency rather than open on integrity" — but the implementation did the
// opposite: a runtime-constructed event with an unrecognised type (e.g.
// from a deserialised RPC payload) would be batched and could be lost on
// crash. The closed-set CI test catches *constants* added without
// classification, but cannot catch values constructed from arbitrary
// strings at runtime.
//
// New contract: known telemetry types batch; everything else (known
// security types AND unknown types) flushes synchronously. This errs on
// the side of integrity. The cost is one extra fsync per unrecognised
// event; in the steady state every event type is one of the constants
// defined above, so the path is only exercised under operator error.
//
// PR #234 review L-6: exported so server.emitAudit can branch on the
// classification when choosing log severity for emit failures (security-
// class → Warn, telemetry-class → Debug).
func IsSecurityEvent(t AuditEventType) bool {
	if _, ok := telemetryEvents[t]; ok {
		return false
	}
	return true
}

// AuditEvent is the wire shape written to the audit sink (one JSON line per event).
//
// Field ordering is alphabetical when serialized — see [canonicalEventJSON] for
// the exact contract used by the checksum chain.
//
// CorrelationID format (RFC 0009 §G + Open Question 6):
//
//	WorkflowRunID:StepID:AgentID:InteractionID
//
// The fourth segment is OPTIONAL — empty when no interaction is open. The
// trailing colon is preserved on the wire so downstream tooling can rely on
// a fixed 4-field parse contract regardless of interaction presence.
type AuditEvent struct {
	Timestamp     time.Time      `json:"timestamp"`
	CorrelationID string         `json:"correlation_id"`
	EventType     AuditEventType `json:"event_type"`
	AgentID       string         `json:"agent_id"`
	Action        string         `json:"action"`
	// Resource identifies the object the event acts upon. Semantics
	// vary by event type and the heterogeneity is intentional (PR #234
	// review L-2):
	//
	//   - `agent.registered` / `tool.invoked` carry the agent_id — the
	//     stable forensic anchor that joins these events to downstream
	//     records emitted on the same agent's behalf.
	//   - `capability.violation` carries the literal `"capability"` —
	//     the event is *about* the capability subsystem rather than a
	//     specific named resource (the offending capability string
	//     lives in `Detail["capability"]`). Cleaning this up to also
	//     carry agent_id was considered but rejected as churn that
	//     touches three emit sites for cosmetic improvement; the
	//     forensic linkage is already provided by the AgentID field
	//     and the CorrelationID's third segment.
	//   - `chain.bootstrap` / `chain.restart` / `chain.recovered`
	//     leave Resource empty — these are sink-lifecycle events with
	//     no acted-upon resource.
	//
	// Future emit sites should pick the agent_id form unless they are
	// genuinely subsystem-scoped (the capability.violation case).
	// Consumers that need a uniform key should join on AgentID, not
	// Resource.
	Resource string         `json:"resource"`
	Outcome  string         `json:"outcome"`
	Detail   map[string]any `json:"detail,omitempty"`
	Checksum string         `json:"checksum"`
}

// CorrelationID composes the canonical 4-field correlation identifier.
//
// Pass an empty string for interactionID when no interaction is open; the
// trailing colon is preserved (`run:step:agent:`) per the RFC 0009 §G fixed
// 4-field parse contract.
func CorrelationID(workflowRunID, stepID, agentID, interactionID string) string {
	return workflowRunID + ":" + stepID + ":" + agentID + ":" + interactionID
}
