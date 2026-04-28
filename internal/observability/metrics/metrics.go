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
	"os"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.uber.org/zap"
)

const (
	defaultServiceName    = "persatrix-server"
	defaultOTLPEndpoint   = "http://localhost:4318"
	defaultExportInterval = 60 * time.Second
	defaultExportTimeout  = 10 * time.Second
)

// Config defines metrics startup settings.
type Config struct {
	ServiceName    string
	Environment    string
	OTLPEndpoint   string
	ExportInterval time.Duration
	ExportTimeout  time.Duration
	InsecureOTLP   bool
}

// NewConfigFromEnv builds metrics config from OTEL_* environment variables.
// Kept symmetrical with observability.NewConfigFromEnv so both initialisers
// behave the same under “OTEL_EXPORTER_OTLP_ENDPOINT“ etc.
func NewConfigFromEnv(environment string) Config {
	cfg := Config{
		ServiceName:    envOrDefault("OTEL_SERVICE_NAME", defaultServiceName),
		Environment:    environment,
		OTLPEndpoint:   envOrDefault("OTEL_EXPORTER_OTLP_ENDPOINT", defaultOTLPEndpoint),
		ExportInterval: defaultExportInterval,
		ExportTimeout:  defaultExportTimeout,
	}
	if s := os.Getenv("OTEL_METRIC_EXPORT_INTERVAL"); s != "" {
		if d, err := time.ParseDuration(s); err == nil && d > 0 {
			cfg.ExportInterval = d
		}
	}
	if s := os.Getenv("OTEL_METRIC_EXPORT_TIMEOUT"); s != "" {
		if d, err := time.ParseDuration(s); err == nil && d > 0 {
			cfg.ExportTimeout = d
		}
	}
	if s := os.Getenv("OTEL_EXPORTER_OTLP_INSECURE"); s != "" {
		if b, err := strconv.ParseBool(s); err == nil {
			cfg.InsecureOTLP = b
		}
	}
	if strings.HasPrefix(cfg.OTLPEndpoint, "http://") {
		cfg.InsecureOTLP = true
	}
	return cfg
}

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
		cfg.ServiceName = defaultServiceName
	}
	if cfg.OTLPEndpoint == "" {
		cfg.OTLPEndpoint = defaultOTLPEndpoint
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

// isLoopbackEndpoint returns true when the endpoint host is localhost or a
// loopback IP literal.  Used by Init to suppress the cleartext-transport
// warning for the documented dev default.  Conservative: anything we cannot
// classify (unparseable, non-loopback) is treated as remote so the warning
// fires.
func isLoopbackEndpoint(endpoint string) bool {
	host := trimScheme(endpoint)
	// Strip any trailing path before splitting host:port.
	if i := strings.IndexByte(host, '/'); i >= 0 {
		host = host[:i]
	}
	if i := strings.LastIndexByte(host, ':'); i >= 0 {
		host = host[:i]
	}
	host = strings.TrimSpace(host)
	switch host {
	case "localhost", "127.0.0.1", "::1", "[::1]":
		return true
	}
	return false
}

// trimScheme strips the URL scheme from an endpoint because otlpmetrichttp
// expects a host:port (it adds the /v1/metrics path itself).  Matches the
// tracing package's approach of accepting a full URL from the env var and
// normalising here rather than forcing operators to think about it.
//
// PR-170 N4: also strips a trailing “/v1/metrics“ (and any leftover slash)
// so an operator who sets “OTEL_EXPORTER_OTLP_ENDPOINT“ to a fully
// path-qualified URL (a common mistake when copying the value used for the
// HTTP traces exporter, which DOES want the full URL) does not end up with
// the otlpmetrichttp client posting to “/v1/metrics/v1/metrics“.  Mirrors
// the equivalent normalisation in agents/observability/metrics.py so both
// runtimes accept the same env-var spelling.
func trimScheme(endpoint string) string {
	trimmed := endpoint
	switch {
	case strings.HasPrefix(trimmed, "http://"):
		trimmed = strings.TrimPrefix(trimmed, "http://")
	case strings.HasPrefix(trimmed, "https://"):
		trimmed = strings.TrimPrefix(trimmed, "https://")
	}
	trimmed = strings.TrimRight(trimmed, "/")
	trimmed = strings.TrimSuffix(trimmed, "/v1/metrics")
	return strings.TrimRight(trimmed, "/")
}

func envOrDefault(key, fallback string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}
