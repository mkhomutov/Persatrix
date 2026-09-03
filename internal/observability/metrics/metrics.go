// Package metrics configures OpenTelemetry metrics for the orchestrator
// (RFC 0019 § F / PR 3).
//
// Mirrors internal/observability/telemetry.go: the same OTLP HTTP endpoint,
// the same Resource attribute set, and the same set of env vars (with one
// additional OTEL_METRIC_EXPORT_INTERVAL env var for the periodic reader
// interval).  The instrument inventory is orchestrator-side only; Python
// agent-side metrics live in agents/observability/metrics.py.
package metrics

import (
	"context"
	"fmt"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability"
)

// Startup Config + the OTEL_* env parsing live in metrics_config.go (split at
// the 500-line review cap).

// Instruments holds the orchestrator-side OTEL metric handles.
//
// Names follow the documented RFC 0019 § F scheme; the corresponding test
// verifies every field is registered with the exact name + unit.
type Instruments struct {
	WorkflowSubmitted metric.Int64Counter
	WorkflowCompleted metric.Int64Counter
	WorkflowFailed    metric.Int64Counter
	WorkflowActive    metric.Int64UpDownCounter
	WorkflowDuration  metric.Float64Histogram
	StepDispatched    metric.Int64Counter
	StepDuration      metric.Float64Histogram

	// RFC 0008 PR 3a — delegation merge counters.
	//
	// Back-fills the orchestrator-side metric inventory deferred from PR 3
	// per the sizing-risk note in docs/rfcs/0008-pr-plan.md.  The Python
	// merge engine (agents/sub_agents/merge.py) emits structured logs
	// named ``delegation_metric`` carrying ``{metric, labels, value}``.
	// Registering the counters here documents the orchestrator-side
	// inventory and lets dashboards target stable instrument names; the
	// log → counter ingestion bridge lands alongside the agent-log
	// ingestion path (LogServiceServer) when delegation traffic is wired
	// across the gRPC boundary.
	DelegationMergeOutcome           metric.Int64Counter
	DelegationMemoryWritesAdmitted   metric.Int64Counter
	DelegationMemoryWritesRejected   metric.Int64Counter
	DelegationMemoryWritesDownscaled metric.Int64Counter

	// RFC 0008 PR 5 — procedural-tier confidence-decay observability.
	//
	// Counters are incremented by the Python eviction loop / facade via
	// the same structured-log → metric bridge as the delegation
	// counters above.  Gauges are intended to be polled directly from
	// the agent process when the LogServiceServer ingests the
	// ``memory.snapshot`` periodic event (PR 6); shipped here so the
	// inventory is stable for dashboards and the v0.3.0 release notes.
	MemoryEvictionsCount              metric.Int64Counter
	MemoryAvgConfidenceAtEviction     metric.Float64Histogram
	MemoryAvgImportanceAtEviction     metric.Float64Histogram
	MemoryUtilizationRatio            metric.Float64Gauge
	MemoryOldestSurvivingEntryAgeDays metric.Float64Gauge
	MemoryEntriesBelowStaleThreshold  metric.Int64Gauge
	MemoryStaleMemoryInjection        metric.Int64Counter

	// RFC 0009 PR 1c — audit-logger observability surface.
	//
	// Names follow the documented `orchestrator.<area>.<noun>` scheme
	// (RFC 0019 §F): event/class-labelled emit counter, dedicated
	// chain-recovery counter, and emit-latency histogram. The histogram
	// drives the capability-fsync amplification SLO documented in
	// docs/observability.md §13 (PR #234 review Medium-1).
	AuditEventsTotal         metric.Int64Counter
	AuditChainRecoveredTotal metric.Int64Counter
	AuditEmitLatencySeconds  metric.Float64Histogram
	// ChannelMessagesDelivered — per-subscriber channel-router dispatch attempts, labelled by `channel_type` + `status` (RFC 0011 §C / RFC 0019 §F); see channel_instruments.go.
	ChannelMessagesDelivered metric.Int64Counter
	// ChannelMessagesPublished pairs with ChannelMessagesDelivered for the delivered/published ratio (ISSUE-0013).
	ChannelMessagesPublished metric.Int64Counter
	// ChannelMessagesCascadeCapped — RFC 0011 cascade-depth amendment; see channel_instruments.go.
	ChannelMessagesCascadeCapped metric.Int64Counter
	// ChannelConversation* — RFC 0030 Layer 2.5 floor-control + v0.3.8 governance-layer + RFC 0052 synthesis-turn telemetry; see channel_instruments.go.
	ChannelConversationFloorTurn            metric.Int64Counter
	ChannelConversationFloorRoundDuration   metric.Float64Histogram
	ChannelConversationGovernanceDrop       metric.Int64Counter
	ChannelConversationInteractionClosed    metric.Int64Counter
	ChannelConversationEndVoteEmitted       metric.Int64Counter
	ChannelConversationReplyBudgetRemaining metric.Float64Histogram
	ChannelConversationChairEscalation      metric.Int64Counter
	ChannelConversationCloseNotification    metric.Int64Counter
	ChannelConversationSynthesisTurn        metric.Int64Counter
	ChannelConversationConvenerAdvance      metric.Int64Counter
	// ChannelConversationSynthesisReserveClamped — ISSUE-0082 residuals PR 4b; see channel_instruments.go.
	ChannelConversationSynthesisReserveClamped metric.Int64Counter
	// ChannelConversationInteractionCapUtilization — ISSUE-0109 calibration series (spend-at-close / cap); see channel_instruments.go.
	ChannelConversationInteractionCapUtilization metric.Float64Histogram
	// SessionsWrites — RFC 0031 Phase 1; see channel_instruments.go.
	SessionsWrites metric.Int64Counter
}

// NewInstruments registers every instrument against the provided meter.
// Returns a wrapped error on the first registration failure so callers can
// fail startup cleanly.
func NewInstruments(m metric.Meter) (*Instruments, error) {
	i := &Instruments{}
	var err error

	if i.WorkflowSubmitted, err = m.Int64Counter(
		"orchestrator.workflow.submitted",
		metric.WithUnit("{workflow}"),
		metric.WithDescription("Workflows accepted by the orchestrator."),
	); err != nil {
		return nil, fmt.Errorf("create workflow.submitted: %w", err)
	}
	if i.WorkflowCompleted, err = m.Int64Counter(
		"orchestrator.workflow.completed",
		metric.WithUnit("{workflow}"),
		metric.WithDescription("Workflows that finished successfully."),
	); err != nil {
		return nil, fmt.Errorf("create workflow.completed: %w", err)
	}
	if i.WorkflowFailed, err = m.Int64Counter(
		"orchestrator.workflow.failed",
		metric.WithUnit("{workflow}"),
		metric.WithDescription("Workflows that terminated with an error."),
	); err != nil {
		return nil, fmt.Errorf("create workflow.failed: %w", err)
	}
	// PR-170 M1: this instrument was originally registered as the bare
	// ``workflow.active`` name — the only Go instrument missing the
	// ``orchestrator.`` prefix used by every other workflow / step metric.
	// That broke the documented namespace and caused dashboards filtered on
	// ``orchestrator.workflow.*`` to silently drop the active-workflow gauge.
	// Renamed to ``orchestrator.workflow.active`` so the inventory is
	// internally consistent and the RFC 0019 § F naming convention holds.
	if i.WorkflowActive, err = m.Int64UpDownCounter(
		"orchestrator.workflow.active",
		metric.WithUnit("{workflow}"),
		metric.WithDescription("Workflows currently executing."),
	); err != nil {
		return nil, fmt.Errorf("create workflow.active: %w", err)
	}
	if i.WorkflowDuration, err = m.Float64Histogram(
		"orchestrator.workflow.duration",
		metric.WithUnit("ms"),
		metric.WithDescription("End-to-end workflow execution duration."),
	); err != nil {
		return nil, fmt.Errorf("create workflow.duration: %w", err)
	}
	if i.StepDispatched, err = m.Int64Counter(
		"orchestrator.step.dispatched",
		metric.WithUnit("{step}"),
		metric.WithDescription("Workflow steps dispatched to an agent."),
	); err != nil {
		return nil, fmt.Errorf("create step.dispatched: %w", err)
	}
	if i.StepDuration, err = m.Float64Histogram(
		"orchestrator.step.duration",
		metric.WithUnit("ms"),
		metric.WithDescription("Per-step dispatch + execute wall-clock duration."),
	); err != nil {
		return nil, fmt.Errorf("create step.duration: %w", err)
	}

	// RFC 0008 PR 3a — delegation merge counters.  The Go counter
	// names mirror the metric strings emitted by the Python merge
	// engine (agents/sub_agents/merge.py) under the
	// ``orchestrator.delegation.`` namespace prefix — e.g. the Python
	// log ``metric=delegation_merge_outcome`` maps to the Go counter
	// ``orchestrator.delegation.merge_outcome``.  The future log →
	// counter bridge therefore needs a fixed-prefix translation, not
	// an opaque lookup table.  Stripping the prefix from the Go names
	// would break the existing OTEL ``orchestrator.<area>.<noun>``
	// naming convention (RFC 0019 § F), so the prefix stays.
	//
	// PR #224 review (Must #2): prior wording claimed a "one-to-one
	// lookup" — corrected because the Python strings carry no
	// namespace and the bridge must prepend ``orchestrator.``.
	if i.DelegationMergeOutcome, err = m.Int64Counter(
		"orchestrator.delegation.merge_outcome",
		metric.WithUnit("{result}"),
		metric.WithDescription(
			"DelegationResult merge outcomes labelled by overall status (completed|partial|failed).",
		),
	); err != nil {
		return nil, fmt.Errorf("create delegation.merge_outcome: %w", err)
	}
	if i.DelegationMemoryWritesAdmitted, err = m.Int64Counter(
		"orchestrator.delegation.memory_writes_admitted",
		metric.WithUnit("{entry}"),
		metric.WithDescription(
			"MemoryWriteEntry items admitted by the merge engine and persisted to caller memory.",
		),
	); err != nil {
		return nil, fmt.Errorf("create delegation.memory_writes_admitted: %w", err)
	}
	if i.DelegationMemoryWritesRejected, err = m.Int64Counter(
		"orchestrator.delegation.memory_writes_rejected",
		metric.WithUnit("{entry}"),
		metric.WithDescription(
			"MemoryWriteEntry items rejected by the merge engine, labelled by reason "+
				"(schema_invalid|cap_exceeded|source_agent_set|procedural_tier_rejected|reserved_tag_prefix|conflict).",
		),
	); err != nil {
		return nil, fmt.Errorf("create delegation.memory_writes_rejected: %w", err)
	}
	if i.DelegationMemoryWritesDownscaled, err = m.Int64Counter(
		"orchestrator.delegation.memory_writes_downscaled",
		metric.WithUnit("{entry}"),
		metric.WithDescription(
			"MemoryWriteEntry items admitted with importance downscaled to the caller's trust_ceiling.",
		),
	); err != nil {
		return nil, fmt.Errorf("create delegation.memory_writes_downscaled: %w", err)
	}

	// RFC 0008 PR 5 — procedural-tier observability.  Names follow the
	// ``orchestrator.memory.*`` namespace per RFC 0019 § F; the Python
	// agent emits matching structured-log events (e.g. log
	// ``metric=stale_memory_injection``) which the LogServiceServer
	// ingestion bridge translates to counter increments after
	// prepending the ``orchestrator.memory.`` prefix — same one-line
	// translation rule as the delegation counters above.
	if i.MemoryEvictionsCount, err = m.Int64Counter(
		"orchestrator.memory.evictions_count",
		metric.WithUnit("{entry}"),
		metric.WithDescription(
			"Memory entries evicted by the periodic loop, labelled by tier (episodic|procedural) and reason (ttl|cap|decay).",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.evictions_count: %w", err)
	}
	if i.MemoryAvgConfidenceAtEviction, err = m.Float64Histogram(
		"orchestrator.memory.average_confidence_at_eviction",
		metric.WithUnit("1"),
		metric.WithDescription(
			"Decayed confidence value of procedural entries at the moment of eviction.",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.average_confidence_at_eviction: %w", err)
	}
	if i.MemoryAvgImportanceAtEviction, err = m.Float64Histogram(
		"orchestrator.memory.average_importance_at_eviction",
		metric.WithUnit("1"),
		metric.WithDescription(
			"Importance value of episodic entries at the moment of eviction.",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.average_importance_at_eviction: %w", err)
	}
	if i.MemoryUtilizationRatio, err = m.Float64Gauge(
		"orchestrator.memory.memory_utilization_ratio",
		metric.WithUnit("1"),
		metric.WithDescription(
			"Per-agent episodic-tier fill ratio (count / episodic_cap).",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.memory_utilization_ratio: %w", err)
	}
	if i.MemoryOldestSurvivingEntryAgeDays, err = m.Float64Gauge(
		"orchestrator.memory.oldest_surviving_entry_age_days",
		metric.WithUnit("d"),
		metric.WithDescription(
			"Age in days of the oldest non-evicted episodic entry per agent.",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.oldest_surviving_entry_age_days: %w", err)
	}
	if i.MemoryEntriesBelowStaleThreshold, err = m.Int64Gauge(
		"orchestrator.memory.entries_below_stale_threshold",
		metric.WithUnit("{entry}"),
		metric.WithDescription(
			"Per-agent count of procedural entries with decayed_confidence < stale_confidence_alert_threshold.",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.entries_below_stale_threshold: %w", err)
	}
	if i.MemoryStaleMemoryInjection, err = m.Int64Counter(
		"orchestrator.memory.stale_memory_injection",
		metric.WithUnit("{event}"),
		metric.WithDescription(
			"Procedural-tier admissions whose decayed_confidence fell into [c_min, stale_confidence_alert_threshold).",
		),
	); err != nil {
		return nil, fmt.Errorf("create memory.stale_memory_injection: %w", err)
	}

	if err := registerAuditInstruments(m, i); err != nil {
		return nil, err
	}

	if err := registerChannelInstruments(m, i); err != nil {
		return nil, err
	}
	return i, nil
}

// Init configures the global OTEL meter provider, registers the orchestrator
// instrument inventory, and returns the instrument bag + a shutdown function
// that flushes pending exports.
//
// The returned shutdown func is safe to call via defer even on partial
// init failures — nil shutdown is handled by main().
func Init(
	ctx context.Context,
	cfg Config,
	logger *zap.Logger,
) (*Instruments, func(context.Context) error, error) {
	if logger == nil {
		logger = zap.NewNop()
	}
	if cfg.ServiceName == "" {
		cfg.ServiceName = observability.DefaultServiceName
	}
	if cfg.OTLPEndpoint == "" {
		cfg.OTLPEndpoint = observability.DefaultOTLPEndpoint
	}

	exporterOpts := []otlpmetrichttp.Option{
		otlpmetrichttp.WithEndpoint(trimScheme(cfg.OTLPEndpoint)),
		otlpmetrichttp.WithTimeout(cfg.ExportTimeout),
	}
	if cfg.InsecureOTLP {
		exporterOpts = append(exporterOpts, otlpmetrichttp.WithInsecure())
	}
	exporter, err := otlpmetrichttp.New(ctx, exporterOpts...)
	if err != nil {
		return nil, nil, fmt.Errorf("create OTLP metric exporter: %w", err)
	}

	res, err := resource.New(
		ctx,
		resource.WithAttributes(
			semconv.ServiceName(cfg.ServiceName),
			semconv.DeploymentEnvironment(cfg.Environment),
		),
	)
	if err != nil {
		// PR-170 S1: the OTLP exporter has already been constructed at this
		// point and owns a background HTTP client + goroutine.  Returning
		// without shutting it down leaks both for the lifetime of the
		// process.  Use a fresh context (the caller's may be the one that
		// just timed out building the resource) and ignore the shutdown
		// error — we are already on the failure path and the wrapped
		// resource error is what callers need to see.
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ExportTimeout)
		defer cancel()
		_ = exporter.Shutdown(shutdownCtx)
		return nil, nil, fmt.Errorf("build OTEL resource: %w", err)
	}

	reader := sdkmetric.NewPeriodicReader(
		exporter,
		sdkmetric.WithInterval(cfg.ExportInterval),
		sdkmetric.WithTimeout(cfg.ExportTimeout),
	)

	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(reader),
	)
	otel.SetMeterProvider(mp)

	meter := mp.Meter("persatrix")
	inst, err := NewInstruments(meter)
	if err != nil {
		_ = mp.Shutdown(ctx)
		return nil, nil, err
	}

	logger.Info("metrics initialized",
		zap.String("serviceName", cfg.ServiceName),
		zap.String("environment", cfg.Environment),
		zap.String("otlpEndpoint", cfg.OTLPEndpoint),
		zap.Duration("exportInterval", cfg.ExportInterval),
	)

	// PR-170 N5: defence-in-depth log when the operator points the exporter
	// at a non-loopback host over plain HTTP.  ``cfg.InsecureOTLP`` is
	// auto-flipped on by NewConfigFromEnv when the endpoint scheme is
	// ``http://`` — that is fine for the documented localhost dev default,
	// but a remote ``http://collector:4318`` ships metrics (including agent
	// IDs and workflow IDs) over the wire in cleartext.  We do not block
	// startup (operator may run in a trusted L2) but we do surface it so
	// the misconfiguration shows up in deployment logs.
	if cfg.InsecureOTLP && !isLoopbackEndpoint(cfg.OTLPEndpoint) {
		logger.Warn("OTLP metrics exporter using plaintext HTTP to a non-loopback host; metric attributes will travel in cleartext",
			zap.String("otlpEndpoint", cfg.OTLPEndpoint),
		)
	}

	return inst, mp.Shutdown, nil
}

// isLoopbackEndpoint / trimScheme / envOrDefault live in metrics_config.go
// with the Config they serve.
