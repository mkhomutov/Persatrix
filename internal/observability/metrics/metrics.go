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
// behave the same under ``OTEL_EXPORTER_OTLP_ENDPOINT`` etc.
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
	if i.WorkflowActive, err = m.Int64UpDownCounter(
		"workflow.active",
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

	return inst, mp.Shutdown, nil
}

// trimScheme strips the URL scheme from an endpoint because otlpmetrichttp
// expects a host:port (it adds the /v1/metrics path itself).  Matches the
// tracing package's approach of accepting a full URL from the env var and
// normalising here rather than forcing operators to think about it.
func trimScheme(endpoint string) string {
	if strings.HasPrefix(endpoint, "http://") {
		return strings.TrimPrefix(endpoint, "http://")
	}
	if strings.HasPrefix(endpoint, "https://") {
		return strings.TrimPrefix(endpoint, "https://")
	}
	return endpoint
}

func envOrDefault(key, fallback string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}
