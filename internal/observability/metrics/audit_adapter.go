package metrics

import (
	"context"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"

	"github.com/mkhomutov/persatrix/internal/security"
)

// auditMetricsAdapter implements [security.AuditMetrics] against the
// orchestrator's [Instruments] inventory. It is the only OTEL-aware
// glue between the security package and this metrics package — keeping
// the OTEL imports out of `internal/security` so audit-logger consumers
// (tests, future SIEM transports) do not transitively pull in the SDK.
type auditMetricsAdapter struct {
	events    metric.Int64Counter
	recovered metric.Int64Counter
	latency   metric.Float64Histogram
}

// NewAuditMetrics returns a [security.AuditMetrics] backed by the
// audit-logger instruments registered on inst. inst must be non-nil;
// the orchestrator constructs it via [NewInstruments]. Callers that
// disable metrics (init failure, opt-out) should pass a nil
// [security.AuditMetrics] into [security.WithAuditMetrics] — the audit
// logger handles that path with a zero-cost no-op surface.
func NewAuditMetrics(inst *Instruments) security.AuditMetrics {
	return &auditMetricsAdapter{
		events:    inst.AuditEventsTotal,
		recovered: inst.AuditChainRecoveredTotal,
		latency:   inst.AuditEmitLatencySeconds,
	}
}

// RecordEvent is invoked once per successful Emit. The label set is
// kept deliberately narrow — `event_type` covers the per-type SLOs and
// `class` lets dashboards split security vs telemetry without joining
// the closed-set classifier table from the security package.
//
// PR #236 review L-3: `context.Background()` is used here for two
// reasons that are easy to confuse:
//
//   - [ObserveEmitLatency] runs as a deferred call in
//     [security.fileAuditLogger.Emit] — by the time the histogram
//     observation fires, `Emit` has already returned `mu.Unlock`'d, so
//     the caller's context may be cancelled or the surrounding span
//     may have closed. A captured caller context would race with that
//     unwind. Detaching to Background avoids the race.
//   - [RecordEvent] is invoked under the audit logger's mutex, before
//     the deferred latency observation; it could in principle thread
//     the caller context. We use Background here too for symmetry and
//     to ensure a stalled metrics export cannot propagate cancellation
//     up into the audit fsync that holds the integrity guarantee.
//
// In short: the detachment lives in this package's contract with the
// audit logger, not inside `Emit` itself (which never wraps the caller
// context with `context.WithoutCancel`).
func (a *auditMetricsAdapter) RecordEvent(eventType security.AuditEventType, class string) {
	a.events.Add(context.Background(), 1,
		metric.WithAttributes(
			attribute.String("event_type", string(eventType)),
			attribute.String("class", class),
		),
	)
}

// RecordChainRecovered is the dedicated counter that drives integrity
// alerting. Operators page on any non-zero increment over a 5-minute
// window — chain-recovery is rare in practice (one-per-restart at most)
// and a sustained increase indicates either repeated crashes mid-write
// or active log tampering.
func (a *auditMetricsAdapter) RecordChainRecovered() {
	a.recovered.Add(context.Background(), 1)
}

// ObserveEmitLatency feeds the emit-latency histogram. Bucket
// boundaries are set in [NewInstruments]; callers do not need to know
// them.
func (a *auditMetricsAdapter) ObserveEmitLatency(d time.Duration) {
	a.latency.Record(context.Background(), d.Seconds())
}
