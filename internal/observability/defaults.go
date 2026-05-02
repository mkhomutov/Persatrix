package observability

// Shared OTLP defaults — referenced by both telemetry.go and
// internal/observability/metrics/metrics.go to avoid drift.  The package
// doc comment lives on telemetry.go (Go convention: one package doc per
// package; staticcheck ST1000 flags duplicates).
const (
	DefaultServiceName  = "persatrix-server"
	DefaultOTLPEndpoint = "http://localhost:4318"
)
