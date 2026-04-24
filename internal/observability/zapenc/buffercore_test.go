// Tests for BufferCore — the zapcore.Core that mirrors orchestrator
// zap entries into the in-process logbuffer.Buffer (RFC 0018 PR 5
// completion: orchestrator self-ingest so `persatrix logs` returns
// orchestrator-emitted records, not just agent-shipped ones).
package zapenc

import (
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// fakeSink captures every Append for assertion.
type fakeSink struct {
	entries []logbuffer.Entry
}

func (f *fakeSink) Append(e logbuffer.Entry) logbuffer.DropReason {
	f.entries = append(f.entries, e)
	return logbuffer.DropNone
}

func newTeedLogger(t *testing.T, sink BufferSink, level zapcore.Level) *zap.Logger {
	t.Helper()
	core := NewBufferCore(sink, "orchestrator", "test-host", level)
	return zap.New(core, zap.AddCaller())
}

func TestBufferCore_PromotesReservedFields(t *testing.T) {
	sink := &fakeSink{}
	logger := newTeedLogger(t, sink, zap.DebugLevel)

	logger.Info("executing run",
		zap.String("execution_id", "exec-1"),
		zap.String("step_id", "plan"),
		zap.String("agent_id", "planner"),
		zap.String("trace_id", "trace-abc"),
		zap.String("span_id", "span-xyz"),
		zap.String("request_id", "req-9"),
		zap.Int("stage", 0),
	)

	require.Len(t, sink.entries, 1)
	e := sink.entries[0]

	assert.Equal(t, "1", e.SchemaVersion)
	assert.Equal(t, "INFO", e.Level)
	assert.Equal(t, "orchestrator", e.ServiceKind)
	assert.Equal(t, "test-host", e.ServiceInstance)
	assert.Equal(t, "executing run", e.Message)
	assert.Equal(t, "exec-1", e.ExecutionID)
	assert.Equal(t, "plan", e.StepID)
	assert.Equal(t, "planner", e.AgentID)
	assert.Equal(t, "trace-abc", e.TraceID)
	assert.Equal(t, "span-xyz", e.SpanID)
	assert.Equal(t, "req-9", e.RequestID)

	// Non-reserved field falls into Attributes.
	require.NotNil(t, e.Attributes)
	assert.EqualValues(t, 0, e.Attributes["stage"])

	// AddCaller() populates Source.
	require.NotNil(t, e.Source)
	assert.Contains(t, e.Source.File, "buffercore_test.go")
	assert.NotZero(t, e.Source.Line)
}

func TestBufferCore_LegacyRenamesNormalised(t *testing.T) {
	sink := &fakeSink{}
	logger := newTeedLogger(t, sink, zap.DebugLevel)

	// Call sites that still use the legacy camelCase keys must land in
	// the same reserved slots as the schema-encoded stderr output.
	logger.Info("legacy keys",
		zap.String("runID", "exec-legacy"),
		zap.String("agentID", "agent-legacy"),
		zap.String("workflowID", "feature-builder"),
		zap.String("stepID", "plan"),
	)

	require.Len(t, sink.entries, 1)
	e := sink.entries[0]
	assert.Equal(t, "exec-legacy", e.ExecutionID)
	assert.Equal(t, "agent-legacy", e.AgentID)
	assert.Equal(t, "plan", e.StepID)

	// workflow_id is surfaced under attributes["workflow"] so the
	// REST `--workflow=` filter (filterEntries in logs_handler.go)
	// matches without the call site needing to pre-shape its fields.
	require.NotNil(t, e.Attributes)
	assert.Equal(t, "feature-builder", e.Attributes["workflow"])

	// Legacy keys are removed (not duplicated alongside the schema name).
	_, hasRunID := e.Attributes["runID"]
	assert.False(t, hasRunID, "legacy runID should be consumed, not surfaced under attributes")
}

func TestBufferCore_LevelGate(t *testing.T) {
	sink := &fakeSink{}
	logger := newTeedLogger(t, sink, zap.WarnLevel)

	logger.Debug("dropped-debug")
	logger.Info("dropped-info")
	logger.Warn("kept-warn", zap.String("execution_id", "e"))
	logger.Error("kept-error", zap.String("execution_id", "e"))

	require.Len(t, sink.entries, 2)
	assert.Equal(t, "WARN", sink.entries[0].Level)
	assert.Equal(t, "ERROR", sink.entries[1].Level)
}

func TestBufferCore_WithMergesContextFields(t *testing.T) {
	sink := &fakeSink{}
	base := newTeedLogger(t, sink, zap.DebugLevel)

	scoped := base.With(zap.String("execution_id", "ctx-exec"))
	scoped.Info("scoped emit", zap.String("step_id", "plan"))

	require.Len(t, sink.entries, 1)
	e := sink.entries[0]
	assert.Equal(t, "ctx-exec", e.ExecutionID, "context exec id must be carried into Append")
	assert.Equal(t, "plan", e.StepID)
}

func TestBufferCore_WriteIgnoresAppendDrop(t *testing.T) {
	// A logging call must never propagate a drop reason as an error
	// (would surface as a panic via zap's WriteThenPanic on Sync).
	dropping := droppingSink{reason: logbuffer.DropRateLimit}
	logger := newTeedLogger(t, dropping, zap.DebugLevel)

	require.NotPanics(t, func() {
		logger.Info("rate-limited", zap.String("execution_id", "e"))
	})
}

type droppingSink struct {
	reason logbuffer.DropReason
}

func (d droppingSink) Append(logbuffer.Entry) logbuffer.DropReason { return d.reason }

func TestBufferCore_NilSinkPanics(t *testing.T) {
	require.PanicsWithValue(t, "zapenc.NewBufferCore: sink is nil", func() {
		_ = NewBufferCore(nil, "orchestrator", "h", zap.InfoLevel)
	})
}

func TestBufferCore_TimestampDefaultsToNowWhenZero(t *testing.T) {
	sink := &fakeSink{}
	core := NewBufferCore(sink, "orchestrator", "h", zap.DebugLevel)

	// Hand-built entry with a zero Time exercises the IsZero fallback;
	// real zap calls always populate ent.Time but we defend against a
	// future code path that hands the core a synthetic entry.
	err := core.Write(zapcore.Entry{Level: zapcore.InfoLevel, Message: "z"}, []zapcore.Field{
		zap.String("execution_id", "e"),
	})
	require.NoError(t, err)
	require.Len(t, sink.entries, 1)
	assert.WithinDuration(t, time.Now().UTC(), sink.entries[0].Timestamp, 2*time.Second)
}

// Sanity: Sync is a no-op and never errors.
func TestBufferCore_SyncNoop(t *testing.T) {
	core := NewBufferCore(&fakeSink{}, "orchestrator", "h", zap.InfoLevel)
	require.NoError(t, core.Sync())
}

// Guard: Write returning a non-nil error from a future regression
// would surface as a logger.Sync() error.  We do not wrap Append's
// drop reason as an error today; assert that contract here.
func TestBufferCore_WriteReturnsNoError(t *testing.T) {
	core := NewBufferCore(droppingSink{reason: logbuffer.DropRateLimit}, "o", "h", zap.InfoLevel)
	err := core.Write(zapcore.Entry{Level: zapcore.InfoLevel, Message: "x"},
		[]zapcore.Field{zap.String("execution_id", "e")})
	require.NoError(t, err, "Write must never propagate Append drops as errors")

	// Appease unused-import warnings for errors when the test set is
	// trimmed in the future — keep the helper alive.
	_ = errors.New
}
