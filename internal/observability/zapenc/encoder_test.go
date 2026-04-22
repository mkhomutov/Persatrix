// Tests for the Persatrix-schema zap encoder (RFC 0018 PR 2).
//
// Coverage map (cross-references the PR plan's "Tests" section):
//
//   - JSON shape: every entry contains schema_version, service.kind,
//     service.instance, source (TestEncoder_EmitsRequiredSchemaFields).
//   - Legacy field rename: one sub-test per key in legacyRenames
//     (TestEncoder_RenamesLegacyKeys).
//   - Field emission order: byte-for-byte assertion on a fixed entry
//     (TestEncoder_FieldOrderMatchesSchema).
//   - Redactor invocation: spy increments once per entry
//     (TestEncoder_RedactorCalledOncePerEntry).
//   - Redactor panic safety: panicking redactor does not panic the encoder
//     and the unredacted entry is emitted (TestEncoder_RedactorPanicFallback).
//   - Source field: contains test file path and non-zero line number
//     (TestEncoder_SourceFieldPopulated).
//   - service.role: omitted when empty, present when set
//     (TestEncoder_ServiceRoleOmittedWhenEmpty / _IncludedWhenSet).
//   - Unknown keys: appended after schema fields, alphabetised
//     (TestEncoder_UnknownKeysAppendedAlphabetically).
package zapenc

import (
	"bytes"
	"encoding/json"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// newTestLogger constructs a zap logger writing through the schema encoder
// into an in-memory buffer.  Caller info is enabled so source assertions
// have something to read.
func newTestLogger(t *testing.T, opts Options) (*zap.Logger, *bytes.Buffer) {
	t.Helper()
	buf := &bytes.Buffer{}
	enc := NewEncoder(opts)
	core := zapcore.NewCore(enc, zapcore.AddSync(buf), zapcore.DebugLevel)
	logger := zap.New(core, zap.AddCaller())
	t.Cleanup(func() {
		_ = logger.Sync()
	})
	return logger, buf
}

func defaultOpts() Options {
	return Options{
		ServiceKind:     "orchestrator",
		ServiceInstance: "test-node",
	}
}

// parseLine returns the JSON object on the first line of buf.  Fails the
// test if the buffer is empty or the first line is not a JSON object.
func parseLine(t *testing.T, buf *bytes.Buffer) map[string]any {
	t.Helper()
	line, err := buf.ReadString('\n')
	require.NoError(t, err, "encoder produced no output")
	var record map[string]any
	require.NoError(t, json.Unmarshal([]byte(strings.TrimSpace(line)), &record), "output is not valid JSON: %q", line)
	return record
}

func TestEncoder_EmitsRequiredSchemaFields(t *testing.T) {
	logger, buf := newTestLogger(t, defaultOpts())
	logger.Info("hello")

	record := parseLine(t, buf)
	for _, key := range []string{
		"schema_version",
		"timestamp",
		"level",
		"service.kind",
		"service.instance",
		"message",
		"source",
	} {
		assert.Contains(t, record, key, "missing required schema field %q", key)
	}
	assert.Equal(t, SchemaVersion, record["schema_version"])
	assert.Equal(t, "orchestrator", record["service.kind"])
	assert.Equal(t, "test-node", record["service.instance"])
	assert.Equal(t, "INFO", record["level"])
	assert.Equal(t, "hello", record["message"])
}

func TestEncoder_ServiceRoleOmittedWhenEmpty(t *testing.T) {
	logger, buf := newTestLogger(t, defaultOpts())
	logger.Info("noop")

	record := parseLine(t, buf)
	assert.NotContains(t, record, "service.role", "service.role must be omitted when ServiceRole == \"\"")
}

func TestEncoder_ServiceRoleIncludedWhenSet(t *testing.T) {
	opts := defaultOpts()
	opts.ServiceRole = "coder"
	logger, buf := newTestLogger(t, opts)
	logger.Info("with role")

	record := parseLine(t, buf)
	assert.Equal(t, "coder", record["service.role"])
}

func TestEncoder_RenamesLegacyKeys(t *testing.T) {
	cases := []struct {
		legacy string
		schema string
	}{
		{"runID", "execution_id"},
		{"executionID", "execution_id"},
		{"agentID", "agent_id"},
		{"workflowID", "workflow_id"},
		{"stepID", "step_id"},
	}
	for _, tc := range cases {
		t.Run(tc.legacy+"_to_"+tc.schema, func(t *testing.T) {
			logger, buf := newTestLogger(t, defaultOpts())
			logger.Info("rename", zap.String(tc.legacy, "value-1"))

			record := parseLine(t, buf)
			assert.NotContains(t, record, tc.legacy, "legacy key must be removed")
			assert.Equal(t, "value-1", record[tc.schema], "value must move to schema key")
		})
	}
}

func TestEncoder_RenamePreservesPreExistingSchemaKey(t *testing.T) {
	// When both legacy + schema-name keys are present, the schema key wins;
	// the encoder is the backstop, not an authoritative re-mapper.
	logger, buf := newTestLogger(t, defaultOpts())
	logger.Info("both",
		zap.String("agent_id", "schema-value"),
		zap.String("agentID", "legacy-value"),
	)

	record := parseLine(t, buf)
	assert.Equal(t, "schema-value", record["agent_id"], "pre-existing schema key takes precedence over rename")
	assert.NotContains(t, record, "agentID", "legacy key removed even when not promoted")
}

func TestEncoder_FieldOrderMatchesSchema(t *testing.T) {
	// Construct an entry that exercises every required field plus several
	// optional fields, then assert byte-for-byte that the keys appear in
	// the canonical schema order.
	opts := defaultOpts()
	opts.ServiceRole = "coder"
	enc := NewEncoder(opts)

	entry := zapcore.Entry{
		Level:      zapcore.InfoLevel,
		Time:       time.Date(2026, 4, 22, 18, 30, 0, 0, time.UTC),
		Message:    "ordered",
		Caller:     zapcore.EntryCaller{Defined: true, File: "test.go", Line: 1, Function: "TestFn"},
		LoggerName: "",
	}
	fields := []zapcore.Field{
		zap.String("execution_id", "exec-1"),
		zap.String("step_id", "step-1"),
		zap.String("agent_id", "agent-1"),
		zap.String("workflow_id", "wf-1"),
		zap.String("request_id", "req-1"),
	}

	buf, err := enc.EncodeEntry(entry, fields)
	require.NoError(t, err)
	defer buf.Free()

	out := strings.TrimSpace(buf.String())
	keys := extractKeysInOrder(t, out)

	expected := []string{
		"schema_version",
		"timestamp",
		"level",
		"service.kind",
		"service.instance",
		"message",
		"service.role",
		"execution_id",
		"step_id",
		"agent_id",
		"workflow_id",
		"request_id",
		"source",
	}
	assert.Equal(t, expected, keys, "key order must match schema (RFC 0018 § B)")
}

// extractKeysInOrder returns the JSON object keys in their physical order on
// the wire.  encoding/json's stdlib decoder does not preserve order, so the
// test parses the raw byte sequence with a streaming decoder.
func extractKeysInOrder(t *testing.T, jsonLine string) []string {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(jsonLine))
	tok, err := dec.Token()
	require.NoError(t, err)
	require.Equal(t, json.Delim('{'), tok)
	var keys []string
	for dec.More() {
		k, err := dec.Token()
		require.NoError(t, err)
		keys = append(keys, k.(string))
		// Skip the value (may be a primitive, object, or array).
		var v any
		require.NoError(t, dec.Decode(&v))
	}
	return keys
}

// spyRedactor counts invocations and optionally panics.
type spyRedactor struct {
	calls    int64
	panicMsg string
}

func (s *spyRedactor) Redact(entry map[string]any) map[string]any {
	atomic.AddInt64(&s.calls, 1)
	if s.panicMsg != "" {
		panic(s.panicMsg)
	}
	entry["redacted"] = true
	return entry
}

func TestEncoder_RedactorCalledOncePerEntry(t *testing.T) {
	spy := &spyRedactor{}
	opts := defaultOpts()
	opts.Redactor = spy
	logger, buf := newTestLogger(t, opts)

	logger.Info("first")
	logger.Info("second")
	logger.Info("third")

	assert.Equal(t, int64(3), atomic.LoadInt64(&spy.calls), "redactor called once per entry")

	// And the redacted marker must be on every emitted line.
	for i := 0; i < 3; i++ {
		record := parseLine(t, buf)
		assert.Equal(t, true, record["redacted"], "redactor's mutation must reach the wire")
	}
}

func TestEncoder_RedactorPanicFallback(t *testing.T) {
	// Capture the encoder's stderr-sink panic warning so the test does not
	// pollute the real test stderr with the deliberate fallback message.
	originalSink := stderrSink
	t.Cleanup(func() { stderrSink = originalSink })
	captured := &bytes.Buffer{}
	stderrSink = captured

	spy := &spyRedactor{panicMsg: "deliberate"}
	opts := defaultOpts()
	opts.Redactor = spy
	logger, buf := newTestLogger(t, opts)

	require.NotPanics(t, func() {
		logger.Info("should-survive")
	})

	record := parseLine(t, buf)
	assert.Equal(t, "should-survive", record["message"], "unredacted record must still reach the wire")
	assert.Contains(t, captured.String(), "redactor panicked", "fallback warning must be emitted out-of-band")
	assert.Contains(t, captured.String(), "deliberate", "fallback warning must include the panic value")
}

func TestEncoder_SourceFieldPopulated(t *testing.T) {
	logger, buf := newTestLogger(t, defaultOpts())
	logger.Info("trace-me")

	record := parseLine(t, buf)
	src, ok := record["source"].(map[string]any)
	require.True(t, ok, "source must be a JSON object")
	file, _ := src["file"].(string)
	assert.True(t, strings.HasSuffix(file, "encoder_test.go"), "source.file must reference the call site, got %q", file)
	line, _ := src["line"].(float64)
	assert.Greater(t, line, float64(0), "source.line must be a positive integer")
	fn, _ := src["function"].(string)
	assert.Contains(t, fn, "TestEncoder_SourceFieldPopulated", "source.function must reference the call-site function")
}

func TestEncoder_UnknownKeysAppendedAlphabetically(t *testing.T) {
	logger, buf := newTestLogger(t, defaultOpts())
	logger.Info("unknowns",
		zap.String("zeta", "z"),
		zap.String("alpha", "a"),
		zap.String("mu", "m"),
	)

	out := buf.String()
	keys := extractKeysInOrder(t, strings.TrimSpace(out))

	// The trailing slice must be alpha, mu, zeta — in that order, after all
	// known schema keys.
	var unknownKeys []string
	for _, k := range keys {
		if _, isKnown := fieldOrderIndex[k]; !isKnown {
			unknownKeys = append(unknownKeys, k)
		}
	}
	assert.Equal(t, []string{"alpha", "mu", "zeta"}, unknownKeys, "unknown keys must be sorted alphabetically for byte-stability")
}

func TestEncoder_NoCallerWhenAddCallerOmitted(t *testing.T) {
	// Build a logger *without* zap.AddCaller() — source must be omitted, not
	// emitted with zero values, per the schema's "Optional" contract.
	buf := &bytes.Buffer{}
	enc := NewEncoder(defaultOpts())
	core := zapcore.NewCore(enc, zapcore.AddSync(buf), zapcore.DebugLevel)
	logger := zap.New(core)
	t.Cleanup(func() { _ = logger.Sync() })

	logger.Info("no-caller")

	record := parseLine(t, buf)
	assert.NotContains(t, record, "source", "source must be omitted when entry.Caller.Defined == false")
}

func TestEncoder_CloneIsolatesContext(t *testing.T) {
	// Sanity check: With(...) on a child logger must not leak fields back
	// into the parent.  This guards against a Clone() implementation that
	// shares the inner encoder's accumulated state across loggers.
	opts := defaultOpts()
	logger, buf := newTestLogger(t, opts)

	child := logger.With(zap.String("agent_id", "child-only"))
	child.Info("child")
	logger.Info("parent")

	childRec := parseLine(t, buf)
	parentRec := parseLine(t, buf)

	assert.Equal(t, "child-only", childRec["agent_id"])
	assert.NotContains(t, parentRec, "agent_id", "parent logger must not see child's With() context")
}
