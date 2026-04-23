package zapenc

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

func TestLoggerWithContext_NoSpanReturnsOriginal(t *testing.T) {
	core, recorded := observer.New(zapcore.DebugLevel)
	base := zap.New(core)

	out := LoggerWithContext(context.Background(), base)
	require.Same(t, base, out, "no active span should return the original logger")

	out.Info("hello")
	entries := recorded.All()
	require.Len(t, entries, 1)
	for _, f := range entries[0].Context {
		require.NotEqual(t, "trace_id", f.Key)
		require.NotEqual(t, "span_id", f.Key)
	}
}

func TestLoggerWithContext_NilCtxOrLoggerIsNoop(t *testing.T) {
	core, _ := observer.New(zapcore.DebugLevel)
	base := zap.New(core)

	require.Same(t, base, LoggerWithContext(nil, base)) //nolint:staticcheck
	require.Nil(t, LoggerWithContext(context.Background(), nil))
}

func TestLoggerWithContext_EmitsTraceAndSpanWhenSpanActive(t *testing.T) {
	exporter := tracetest.NewInMemoryExporter()
	tp := trace.NewTracerProvider(trace.WithSyncer(exporter))
	defer func() { _ = tp.Shutdown(context.Background()) }()
	tracer := tp.Tracer("test")

	ctx, span := tracer.Start(context.Background(), "test-span")
	defer span.End()

	core, recorded := observer.New(zapcore.DebugLevel)
	base := zap.New(core)
	logger := LoggerWithContext(ctx, base)
	logger.Info("inside span")

	entries := recorded.All()
	require.Len(t, entries, 1)
	fields := fieldMap(entries[0].Context)
	require.Equal(t, span.SpanContext().TraceID().String(), fields["trace_id"])
	require.Equal(t, span.SpanContext().SpanID().String(), fields["span_id"])
}

func fieldMap(fs []zapcore.Field) map[string]string {
	m := make(map[string]string, len(fs))
	for _, f := range fs {
		m[f.Key] = f.String
	}
	return m
}
