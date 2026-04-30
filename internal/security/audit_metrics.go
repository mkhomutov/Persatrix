package security

import "time"

// AuditMetrics is the optional metrics surface the audit logger publishes
// counters and a latency histogram through (RFC 0009 PR 1c — RFC 0019 §F
// instrument inventory).
//
// The orchestrator wires the OTEL-backed implementation
// ([observability/metrics.NewAuditMetrics]); tests pass nil to opt out.
// Implementations must be safe for concurrent use; the audit logger
// invokes them under its own mutex but the metric backends themselves
// are typically lock-free.
//
// Why an interface here rather than passing OTEL handles directly: the
// `internal/security` package must not import the metrics SDK (it would
// pull every audit consumer into the OTEL build graph and complicate
// unit testing). This three-method surface keeps the dependency
// inversion at the right boundary.
type AuditMetrics interface {
	// RecordEvent is called once per successful Emit, after the line has
	// been written to the buffer (and after fsync for security-class
	// events). class is "security" or "telemetry" per
	// [IsSecurityEvent].
	RecordEvent(eventType AuditEventType, class string)

	// RecordChainRecovered is called when [NewFileAuditLogger] emits a
	// `chain.recovered` synthetic event at startup — i.e. the prior
	// process's tail line was unparseable, truncated, or had a
	// mismatched checksum. Operators alert on this counter rising.
	RecordChainRecovered()

	// ObserveEmitLatency reports the wall-clock duration of a single
	// Emit call (mutex acquisition + serialise + write + maybe fsync).
	// The audit logger holds its mutex through fsync for security-class
	// events, so this histogram is the canonical surface for the
	// capability-fsync amplification SLO documented in
	// docs/observability.md §13 (PR #234 review Medium-1).
	ObserveEmitLatency(d time.Duration)
}

// noopAuditMetrics is the zero-cost default used when [WithAuditMetrics]
// is not supplied. Calls inline-fold to nothing under -gcflags="-m".
type noopAuditMetrics struct{}

func (noopAuditMetrics) RecordEvent(AuditEventType, string) {}
func (noopAuditMetrics) RecordChainRecovered()              {}
func (noopAuditMetrics) ObserveEmitLatency(time.Duration)   {}

// classifyAuditEvent returns the "security" / "telemetry" string label
// used by [AuditMetrics.RecordEvent]. Pulled out so the audit logger
// and any future emitters share one source of truth for the label set.
func classifyAuditEvent(t AuditEventType) string {
	if IsSecurityEvent(t) {
		return "security"
	}
	return "telemetry"
}
