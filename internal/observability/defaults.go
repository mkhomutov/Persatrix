// Package observability configures OpenTelemetry tracing for the orchestrator.
package observability

// Shared OTLP defaults — referenced by both telemetry.go and
// internal/observability/metrics/metrics.go to avoid drift.
const (
	DefaultServiceName  = "persatrix-server"
	DefaultOTLPEndpoint = "http://localhost:4318"
)
