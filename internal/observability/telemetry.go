// Package observability configures OpenTelemetry tracing for the orchestrator.
package observability

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.uber.org/zap"
)

const (
	defaultServiceName  = DefaultServiceName
	defaultOTLPEndpoint = DefaultOTLPEndpoint
)

// Config defines telemetry startup settings.
type Config struct {
	ServiceName  string
	Environment  string
	OTLPEndpoint string
	SampleRatio  float64
	InsecureOTLP bool
}

// NewConfigFromEnv builds telemetry config from OTEL_* environment variables.
func NewConfigFromEnv(environment string) Config {
	cfg := Config{
		ServiceName:  envOrDefault("OTEL_SERVICE_NAME", defaultServiceName),
		Environment:  environment,
		OTLPEndpoint: envOrDefault("OTEL_EXPORTER_OTLP_ENDPOINT", defaultOTLPEndpoint),
		SampleRatio:  1.0,
	}

	if ratioStr := os.Getenv("OTEL_TRACES_SAMPLER_ARG"); ratioStr != "" {
		if ratio, err := strconv.ParseFloat(ratioStr, 64); err == nil && ratio >= 0 && ratio <= 1 {
			cfg.SampleRatio = ratio
		}
	}

	if insecureStr := os.Getenv("OTEL_EXPORTER_OTLP_INSECURE"); insecureStr != "" {
		if insecure, err := strconv.ParseBool(insecureStr); err == nil {
			cfg.InsecureOTLP = insecure
		}
	}

	if strings.HasPrefix(cfg.OTLPEndpoint, "http://") {
		cfg.InsecureOTLP = true
	}

	return cfg
}

// Init configures global OTEL tracing and returns a shutdown function.
func Init(ctx context.Context, cfg Config, logger *zap.Logger) (func(context.Context) error, error) {
	if logger == nil {
		logger = zap.NewNop()
	}

	if cfg.ServiceName == "" {
		cfg.ServiceName = defaultServiceName
	}
	if cfg.OTLPEndpoint == "" {
		cfg.OTLPEndpoint = defaultOTLPEndpoint
	}
	if cfg.SampleRatio < 0 || cfg.SampleRatio > 1 {
		return nil, fmt.Errorf("invalid sample ratio %.4f (must be in [0,1])", cfg.SampleRatio)
	}

	exporterOpts := []otlptracehttp.Option{otlptracehttp.WithEndpointURL(cfg.OTLPEndpoint)}
	if cfg.InsecureOTLP {
		exporterOpts = append(exporterOpts, otlptracehttp.WithInsecure())
	}

	exporter, err := otlptracehttp.New(ctx, exporterOpts...)
	if err != nil {
		return nil, fmt.Errorf("create OTLP HTTP exporter: %w", err)
	}

	res, err := resource.New(
		ctx,
		resource.WithAttributes(
			semconv.ServiceName(cfg.ServiceName),
			semconv.DeploymentEnvironment(cfg.Environment),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("build OTEL resource: %w", err)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithBatcher(exporter, sdktrace.WithBatchTimeout(1*time.Second)),
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.SampleRatio))),
	)

	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	logger.Info("telemetry initialized",
		zap.String("serviceName", cfg.ServiceName),
		zap.String("environment", cfg.Environment),
		zap.String("otlpEndpoint", cfg.OTLPEndpoint),
		zap.Float64("sampleRatio", cfg.SampleRatio),
	)

	return tp.Shutdown, nil
}

func envOrDefault(key, fallback string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}
