// Package metrics tests (RFC 0019 PR 3).
package metrics

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// buildInstruments wires NewInstruments against an in-memory manual reader
// so tests can assert recorded values without standing up an OTLP endpoint.
func buildInstruments(t *testing.T) (*Instruments, *metric.ManualReader) {
	t.Helper()
	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))
	t.Cleanup(func() {
		_ = mp.Shutdown(context.Background())
	})
	inst, err := NewInstruments(mp.Meter("persatrix"))
	require.NoError(t, err)
	return inst, reader
}

func collect(t *testing.T, reader *metric.ManualReader) metricdata.ResourceMetrics {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	return rm
}

func findMetric(rm metricdata.ResourceMetrics, name string) *metricdata.Metrics {
	for i := range rm.ScopeMetrics {
		for j := range rm.ScopeMetrics[i].Metrics {
			m := &rm.ScopeMetrics[i].Metrics[j]
			if m.Name == name {
				return m
			}
		}
	}
	return nil
}

// TestInstrumentInventory asserts every documented orchestrator instrument
// is registered with the exact name + unit documented in RFC 0019 § F.  This
// is the unit-test parity net; PR 4 adds a cross-language schema-parity test.
func TestInstrumentInventory(t *testing.T) {
	inst, reader := buildInstruments(t)

	// Touch every instrument so the reader has data to enumerate.  A zero
	// recording is enough to surface the name + unit.
	ctx := context.Background()
	inst.WorkflowSubmitted.Add(ctx, 0)
	inst.WorkflowCompleted.Add(ctx, 0)
	inst.WorkflowFailed.Add(ctx, 0)
	inst.WorkflowActive.Add(ctx, 0)
	inst.WorkflowDuration.Record(ctx, 0)
	inst.StepDispatched.Add(ctx, 0)
	inst.StepDuration.Record(ctx, 0)

	rm := collect(t, reader)

	expected := map[string]string{
		"orchestrator.workflow.submitted": "{workflow}",
		"orchestrator.workflow.completed": "{workflow}",
		"orchestrator.workflow.failed":    "{workflow}",
		"workflow.active":                 "{workflow}",
		"orchestrator.workflow.duration":  "ms",
		"orchestrator.step.dispatched":    "{step}",
		"orchestrator.step.duration":      "ms",
	}
	for name, unit := range expected {
		m := findMetric(rm, name)
		require.NotNilf(t, m, "metric %s not registered", name)
		assert.Equalf(t, unit, m.Unit, "metric %s unit mismatch", name)
	}
}

// TestCounterMonotonic asserts repeated Add calls accumulate.
func TestCounterMonotonic(t *testing.T) {
	inst, reader := buildInstruments(t)
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		inst.WorkflowSubmitted.Add(ctx, 1)
	}
	rm := collect(t, reader)
	m := findMetric(rm, "orchestrator.workflow.submitted")
	require.NotNil(t, m)
	sum, ok := m.Data.(metricdata.Sum[int64])
	require.True(t, ok, "expected Sum[int64], got %T", m.Data)
	require.Len(t, sum.DataPoints, 1)
	assert.Equal(t, int64(5), sum.DataPoints[0].Value)
}

// TestUpDownGauge asserts the workflow.active gauge can go up and down.
func TestUpDownGauge(t *testing.T) {
	inst, reader := buildInstruments(t)
	ctx := context.Background()
	inst.WorkflowActive.Add(ctx, 3)
	inst.WorkflowActive.Add(ctx, -1)
	rm := collect(t, reader)
	m := findMetric(rm, "workflow.active")
	require.NotNil(t, m)
	sum, ok := m.Data.(metricdata.Sum[int64])
	require.True(t, ok)
	require.Len(t, sum.DataPoints, 1)
	assert.Equal(t, int64(2), sum.DataPoints[0].Value)
}

// TestHistogramBucketsSane records a duration and verifies bucket bounds are
// the OTEL default set (no zero-length bucket array / missing bounds).
func TestHistogramBucketsSane(t *testing.T) {
	inst, reader := buildInstruments(t)
	ctx := context.Background()
	inst.WorkflowDuration.Record(ctx, 123.0)
	rm := collect(t, reader)
	m := findMetric(rm, "orchestrator.workflow.duration")
	require.NotNil(t, m)
	hist, ok := m.Data.(metricdata.Histogram[float64])
	require.True(t, ok, "expected Histogram[float64], got %T", m.Data)
	require.Len(t, hist.DataPoints, 1)
	assert.Greater(t, len(hist.DataPoints[0].Bounds), 0, "no bucket bounds")
	assert.Equal(t, uint64(1), hist.DataPoints[0].Count)
}

// TestNewConfigFromEnv exercises the env-var parsing surface so operators
// get deterministic overrides.
func TestNewConfigFromEnv_Defaults(t *testing.T) {
	cfg := NewConfigFromEnv("dev")
	assert.Equal(t, "persatrix-server", cfg.ServiceName)
	assert.Equal(t, "dev", cfg.Environment)
	assert.Equal(t, "http://localhost:4318", cfg.OTLPEndpoint)
	assert.Greater(t, int64(cfg.ExportInterval), int64(0))
	assert.True(t, cfg.InsecureOTLP, "http:// default should imply insecure")
}

func TestNewConfigFromEnv_Overrides(t *testing.T) {
	t.Setenv("OTEL_SERVICE_NAME", "custom")
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example:4318")
	t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", "5s")
	cfg := NewConfigFromEnv("prod")
	assert.Equal(t, "custom", cfg.ServiceName)
	assert.Equal(t, "https://collector.example:4318", cfg.OTLPEndpoint)
	assert.False(t, cfg.InsecureOTLP, "https:// default should keep TLS on")
	assert.Equal(t, "5s", cfg.ExportInterval.String())
}

// TestTrimScheme sanity-checks the helper used by Init for otlpmetrichttp.
func TestTrimScheme(t *testing.T) {
	assert.Equal(t, "collector:4318", trimScheme("http://collector:4318"))
	assert.Equal(t, "collector:4318", trimScheme("https://collector:4318"))
	assert.Equal(t, "collector:4318", trimScheme("collector:4318"))
}
