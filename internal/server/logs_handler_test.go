// Tests for the REST log retrieval endpoint (RFC 0018 PR 5).
package server

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// testServerWithBuffer creates a Server wired with a logbuffer rooted at
// a per-test temp dir so Snapshot / ListExecutions exercise the real
// disk + ring path without polluting cwd.
func testServerWithBuffer(t *testing.T) (*Server, *logbuffer.Buffer) {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	buf, err := logbuffer.New(logbuffer.Config{
		Dir:           t.TempDir(),
		PerExecution:  100,
		MaxExecutions: 8,
		DiskCapBytes:  1 << 20,
		DropLevel:     "DEBUG",
		RatePerExec:   1000,
	}, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })

	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger, WithLogBuffer(buf))
	require.NoError(t, err)
	return srv, buf
}

func mkEntry(execID, level, msg string, ts time.Time, attrs map[string]any) logbuffer.Entry {
	return logbuffer.Entry{
		SchemaVersion: "0.1",
		Timestamp:     ts,
		Level:         level,
		Message:       msg,
		ExecutionID:   execID,
		Attributes:    attrs,
	}
}

func TestLogs_NoBuffer_Returns501(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs", nil)
	assert.Equal(t, http.StatusNotImplemented, rec.Code)
}

func TestLogs_UnknownExecution_ReturnsEmpty200(t *testing.T) {
	srv, _ := testServerWithBuffer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/never-seen/logs", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	assert.Empty(t, out)
}

func TestLogs_InvalidExecutionID_Returns400(t *testing.T) {
	srv, _ := testServerWithBuffer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/bad..id/logs", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestLogs_SortedAscending(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	// Append out of order; handler must return ascending by timestamp.
	require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry("exec-1", "INFO", "third", now.Add(2*time.Second), nil)))
	require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry("exec-1", "INFO", "first", now, nil)))
	require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry("exec-1", "INFO", "second", now.Add(time.Second), nil)))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs", nil)
	require.Equal(t, http.StatusOK, rec.Code)

	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	require.Len(t, out, 3)
	assert.Equal(t, "first", out[0].Message)
	assert.Equal(t, "second", out[1].Message)
	assert.Equal(t, "third", out[2].Message)
}

func TestLogs_LevelFilter(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	buf.Append(mkEntry("exec-1", "INFO", "i", now, nil))
	buf.Append(mkEntry("exec-1", "ERROR", "e", now.Add(time.Millisecond), nil))
	buf.Append(mkEntry("exec-1", "WARN", "w", now.Add(2*time.Millisecond), nil))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?level=ERROR", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	require.Len(t, out, 1)
	assert.Equal(t, "ERROR", out[0].Level)
}

func TestLogs_WorkflowFilter(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	buf.Append(mkEntry("exec-1", "INFO", "a", now, map[string]any{"workflow": "wf-A"}))
	buf.Append(mkEntry("exec-1", "INFO", "b", now.Add(time.Millisecond), map[string]any{"workflow": "wf-B"}))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?workflow=wf-A", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	require.Len(t, out, 1)
	assert.Equal(t, "a", out[0].Message)
}

func TestLogs_SinceDuration(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	buf.Append(mkEntry("exec-1", "INFO", "old", now.Add(-time.Hour), nil))
	buf.Append(mkEntry("exec-1", "INFO", "new", now, nil))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?since=5m", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	require.Len(t, out, 1)
	assert.Equal(t, "new", out[0].Message)
}

func TestLogs_InvalidSince_Returns400(t *testing.T) {
	srv, _ := testServerWithBuffer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?since=garbage", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// PR #173 review Should-Fix #1: negative durations parse via
// time.ParseDuration but would silently translate to a future `since`
// and always-empty result.  Reject as 400 to surface client typos.
func TestLogs_NegativeSince_Returns400(t *testing.T) {
	srv, _ := testServerWithBuffer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?since=-5m", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestLogs_InvalidLevel_Returns400(t *testing.T) {
	srv, _ := testServerWithBuffer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?level=NOTICE", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestLogs_LimitTruncatesNewest(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	for i := range 5 {
		buf.Append(mkEntry("exec-1", "INFO", string(rune('a'+i)), now.Add(time.Duration(i)*time.Millisecond), nil))
	}

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?limit=2", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	require.Len(t, out, 2)
	// Newest 2 retained → 'd', 'e'.
	assert.Equal(t, "d", out[0].Message)
	assert.Equal(t, "e", out[1].Message)
}

func TestLogs_CrossExecutionMerge(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	buf.Append(mkEntry("exec-A", "INFO", "a1", now, nil))
	buf.Append(mkEntry("exec-B", "INFO", "b1", now.Add(time.Millisecond), nil))
	buf.Append(mkEntry("exec-A", "INFO", "a2", now.Add(2*time.Millisecond), nil))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/_/logs", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var out []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	require.Len(t, out, 3)
	assert.Equal(t, "a1", out[0].Message)
	assert.Equal(t, "b1", out[1].Message)
	assert.Equal(t, "a2", out[2].Message)
}
