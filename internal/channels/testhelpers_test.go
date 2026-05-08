package channels

import (
	"context"
	"os"
	"testing"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// writeFile is a tiny helper shared across the channels test suite. The
// `_test.go` suffix scopes it to test builds; placing it in its own file
// (rather than inside one of the *_test.go files that uses it) avoids a
// compile error when only a subset of tests are built with `go test -run`.
func writeFile(path, body string) error {
	return os.WriteFile(path, []byte(body), 0o600)
}

// TestMain installs a package-wide synchronous in-memory OTEL span
// exporter once for every test in `internal/channels`. The dispatcher's
// `channel.dispatch` span (ISSUE-0032) is asserted via this exporter in
// grpc_dispatcher_test.go; placing the wiring here keeps the OTEL setup
// out of the per-test files and ensures the exporter is live whether
// the suite runs `-run` filtered or in full.
//
// We deliberately do NOT cycle providers per-test — `otel.SetTracerProvider`
// has a `delegateTracerOnce` guard that locks the package-level
// `dispatcherTracer = otel.Tracer(...)` wrapper to the first provider it
// sees. Subsequent SetTracerProvider calls would route later tests' spans
// to a shut-down exporter. Each span-asserting test calls
// `installSpanRecorder(t)` (defined in grpc_dispatcher_test.go) which
// `Reset()`s this exporter so spans from other tests do not leak in.
func TestMain(m *testing.M) {
	channelsTestSpanExporter = tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(channelsTestSpanExporter))
	otel.SetTracerProvider(tp)
	code := m.Run()
	_ = tp.Shutdown(context.Background())
	os.Exit(code)
}
