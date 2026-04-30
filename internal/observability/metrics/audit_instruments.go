package metrics

import (
	"fmt"

	"go.opentelemetry.io/otel/metric"
)

// registerAuditInstruments wires the RFC 0009 PR 1c audit-logger
// observability surface onto i. Split out of [NewInstruments] so the
// audit instruments live with the audit adapter rather than padding
// metrics.go past the 500-line review limit.
//
// Names follow the documented `orchestrator.<area>.<noun>` scheme
// (RFC 0019 §F): event/class-labelled emit counter, dedicated
// chain-recovery counter, and emit-latency histogram. The histogram
// drives the capability-fsync amplification SLO documented in
// docs/observability.md §13 (PR #234 review Medium-1).
func registerAuditInstruments(m metric.Meter, i *Instruments) error {
	var err error
	if i.AuditEventsTotal, err = m.Int64Counter(
		"orchestrator.audit.events_total",
		metric.WithUnit("{event}"),
		metric.WithDescription(
			"AuditEvent records committed to the audit sink, labelled by event_type and class (security|telemetry).",
		),
	); err != nil {
		return fmt.Errorf("create audit.events_total: %w", err)
	}
	if i.AuditChainRecoveredTotal, err = m.Int64Counter(
		"orchestrator.audit.chain_recovered_total",
		metric.WithUnit("{event}"),
		metric.WithDescription(
			"chain.recovered synthetic events emitted at startup when the prior tail line was unparseable, truncated, or had a mismatched checksum.",
		),
	); err != nil {
		return fmt.Errorf("create audit.chain_recovered_total: %w", err)
	}
	// Bucket boundaries are tuned for fsync-bearing emits: the p95 on
	// SSD-backed sinks should sit below 5 ms and the SLO alert
	// (docs/observability.md §13) fires at p95 > 100 ms over 5 min.
	if i.AuditEmitLatencySeconds, err = m.Float64Histogram(
		"orchestrator.audit.emit_latency_seconds",
		metric.WithUnit("s"),
		metric.WithDescription(
			"Wall-clock duration of a single AuditLogger.Emit call, including mutex acquisition + serialise + write + (security-class) fsync.",
		),
		metric.WithExplicitBucketBoundaries(
			0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.000, 2.500,
		),
	); err != nil {
		return fmt.Errorf("create audit.emit_latency_seconds: %w", err)
	}
	return nil
}
