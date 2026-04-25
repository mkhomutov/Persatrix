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
	"testing"

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
