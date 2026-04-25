package zapenc

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

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

// nilReturnRedactor returns nil from Redact — legal Go for map[string]any
// but a footgun for the encoder's schema invariants if not guarded.
type nilReturnRedactor struct{}

func (nilReturnRedactor) Redact(_ map[string]any) map[string]any { return nil }

func TestEncoder_RedactorCalledOncePerEntry(t *testing.T) {
	spy := &spyRedactor{}
	opts := defaultOpts()
	opts.Redactor = spy
	logger, buf := newTestLogger(t, opts)

	logger.Info("first")
	logger.Info("second")
	logger.Info("third")

	assert.Equal(t, int64(3), atomic.LoadInt64(&spy.calls), "redactor called once per entry")

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

// TestEncoder_RedactorNilReturnFallsBackToEntry covers the previously
// unhandled nil-return path (RFC 0018 PR 2 review, Must-Fix #3).  Without
// the `if out == nil { out = entry }` guard in applyRedactor, the resulting
// envelope would lack schema_version / service.* and be silently dropped by
// the downstream `schema_version: "1"` filter.
func TestEncoder_RedactorNilReturnFallsBackToEntry(t *testing.T) {
	opts := defaultOpts()
	opts.Redactor = nilReturnRedactor{}
	logger, buf := newTestLogger(t, opts)

	require.NotPanics(t, func() {
		logger.Info("nil-redactor")
	})

	record := parseLine(t, buf)
	assert.Equal(t, SchemaVersion, record["schema_version"], "schema_version must survive a nil-returning redactor")
	assert.Equal(t, "orchestrator", record["service.kind"], "service.kind must survive a nil-returning redactor")
	assert.Equal(t, "test-node", record["service.instance"], "service.instance must survive a nil-returning redactor")
	assert.Equal(t, "nil-redactor", record["message"], "message must survive a nil-returning redactor")
}

// TestEncoder_JSONUnmarshalFallbackEmitsCompliantEnvelope covers the
// previously untested fallback path (RFC 0018 PR 2 review, Must-Fix #1).
//
// The path is impossible to reach naturally because zap's inner JSON encoder
// always emits valid JSON.  The encoder exposes the parser as a package var
// (jsonUnmarshal) so this test can swap it for one that always errors and
// assert that:
//
//   - the emitted line is still schema-conformant (schema_version: "1",
//     service.kind, service.instance present),
//   - the original raw payload is preserved under attributes.raw (base64),
//   - an out-of-band warning lands on stderrSink.
func TestEncoder_JSONUnmarshalFallbackEmitsCompliantEnvelope(t *testing.T) {
	originalUnmarshal := jsonUnmarshal
	t.Cleanup(func() { jsonUnmarshal = originalUnmarshal })
	jsonUnmarshal = func(data []byte, v any) error {
		return errors.New("forced fallback")
	}

	originalSink := stderrSink
	t.Cleanup(func() { stderrSink = originalSink })
	captured := &bytes.Buffer{}
	stderrSink = captured

	logger, buf := newTestLogger(t, defaultOpts())
	require.NotPanics(t, func() {
		logger.Info("triggers-fallback", zap.String("agent_id", "a-1"))
	})

	record := parseLine(t, buf)
	assert.Equal(t, SchemaVersion, record["schema_version"], "fallback envelope must carry schema_version")
	assert.Equal(t, "orchestrator", record["service.kind"], "fallback envelope must carry service.kind")
	assert.Equal(t, "test-node", record["service.instance"], "fallback envelope must carry service.instance")
	assert.Equal(t, "INFO", record["level"], "fallback envelope must mirror the original entry's level")

	attrs, ok := record["attributes"].(map[string]any)
	require.True(t, ok, "fallback envelope must carry attributes object")
	rawB64, _ := attrs["raw"].(string)
	require.NotEmpty(t, rawB64, "attributes.raw must contain the base64 of the inner encoder output")
	rawBytes, err := base64.StdEncoding.DecodeString(rawB64)
	require.NoError(t, err, "attributes.raw must be valid base64")
	assert.Contains(t, string(rawBytes), "triggers-fallback", "raw payload must round-trip the inner encoder's bytes")
	assert.Equal(t, "forced fallback", attrs["parse_error"], "attributes.parse_error must surface the underlying error")

	assert.Contains(t, captured.String(), "encoder fallback", "out-of-band warning must be emitted on stderrSink")
}

// TestEncoder_JSONUnmarshalFallbackLevelMatchesEntry asserts that the
// envelope's level mirrors the original entry's level rather than always
// being ERROR.
func TestEncoder_JSONUnmarshalFallbackLevelMatchesEntry(t *testing.T) {
	originalUnmarshal := jsonUnmarshal
	t.Cleanup(func() { jsonUnmarshal = originalUnmarshal })
	jsonUnmarshal = func(data []byte, v any) error {
		return errors.New("forced")
	}
	originalSink := stderrSink
	t.Cleanup(func() { stderrSink = originalSink })
	stderrSink = &bytes.Buffer{}

	logger, buf := newTestLogger(t, defaultOpts())
	logger.Warn("warn-line")
	record := parseLine(t, buf)
	assert.Equal(t, "WARN", record["level"], "fallback envelope must mirror the entry's level")
}

// TestEncoder_ConcurrentEncodeIsSafe fans out goroutines through a single
// encoder to surface any race in the package-level pool / stderrSink
// (RFC 0018 PR 2 review, Should-Fix #4).  Run with `go test -race` to be
// meaningful; the assertion below also catches lost lines.
func TestEncoder_ConcurrentEncodeIsSafe(t *testing.T) {
	const goroutines = 64
	const perGoroutine = 25

	buffers := make([]*bytes.Buffer, goroutines)
	loggers := make([]*zap.Logger, goroutines)
	enc := NewEncoder(defaultOpts())
	for i := 0; i < goroutines; i++ {
		buffers[i] = &bytes.Buffer{}
		core := zapcore.NewCore(enc, zapcore.AddSync(buffers[i]), zapcore.DebugLevel)
		loggers[i] = zap.New(core, zap.AddCaller())
	}

	var wg sync.WaitGroup
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func(idx int) {
			defer wg.Done()
			for j := 0; j < perGoroutine; j++ {
				loggers[idx].Info("concurrent",
					zap.Int("goroutine", idx),
					zap.Int("iteration", j),
				)
			}
		}(i)
	}
	wg.Wait()

	for i := 0; i < goroutines; i++ {
		lines := strings.Split(strings.TrimRight(buffers[i].String(), "\n"), "\n")
		assert.Equal(t, perGoroutine, len(lines), "goroutine %d lost lines under concurrency", i)
		for _, line := range lines {
			var rec map[string]any
			require.NoError(t, json.Unmarshal([]byte(line), &rec), "concurrent line not valid JSON: %q", line)
			assert.Equal(t, SchemaVersion, rec["schema_version"], "concurrent line missing schema_version")
		}
	}
}

// TestNewEncoder_PanicsWhenServiceKindEmpty locks in the Must-style
// constructor contract (RFC 0018 PR 2 review, issue #178).
func TestNewEncoder_PanicsWhenServiceKindEmpty(t *testing.T) {
	assert.PanicsWithValue(t,
		"zapenc: Options.ServiceKind must be non-empty (schema required-field group)",
		func() {
			_ = NewEncoder(Options{ServiceKind: "", ServiceInstance: "node-1"})
		},
	)
}

// TestNewEncoder_PanicsWhenServiceInstanceEmpty mirrors the ServiceKind
// contract.
func TestNewEncoder_PanicsWhenServiceInstanceEmpty(t *testing.T) {
	assert.PanicsWithValue(t,
		"zapenc: Options.ServiceInstance must be non-empty (schema required-field group)",
		func() {
			_ = NewEncoder(Options{ServiceKind: "orchestrator", ServiceInstance: ""})
		},
	)
}
