package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// ISSUE-0204: the health check says what the orchestrator runs without.
//
// The orchestrator keeps running when its log buffer cannot start. The
// health check used to answer the same `{"status":"ok"}` either way, so
// nothing but one start-up warning told an operator. It now names the
// missing part, and still answers 200 with `status` "ok": a liveness
// probe that restarted the process would restart it into the same state
// for ever, and agents are served without the buffer.
func TestHealthz_NamesWhatIsMissing(t *testing.T) {
	tests := []struct {
		name          string
		withLogBuffer bool
		// wantBody is the whole body. With nothing missing it is the body
		// the route has always answered, byte for byte.
		wantBody    string
		wantMissing []string
	}{
		{
			name:          "the log buffer started",
			withLogBuffer: true,
			wantBody:      `{"status":"ok"}`,
		},
		{
			name:        "the log buffer did not start",
			wantBody:    `{"status":"ok","missing":["log_buffer"]}`,
			wantMissing: []string{"log_buffer"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logger := zap.NewNop()
			var opts []ServerOption
			if tt.withLogBuffer {
				buf, err := logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, logger)
				require.NoError(t, err)
				t.Cleanup(func() { _ = buf.Close() })
				opts = append(opts, WithLogBuffer(buf))
			}
			srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
				registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger, opts...)
			require.NoError(t, err)

			rec := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)

			assert.Equal(t, http.StatusOK, rec.Code, "a liveness probe must not restart a process that serves agents")
			assert.JSONEq(t, tt.wantBody, rec.Body.String())
			var body struct {
				Status  string   `json:"status"`
				Missing []string `json:"missing"`
			}
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))
			assert.Equal(t, "ok", body.Status)
			assert.Equal(t, tt.wantMissing, body.Missing)
			if tt.wantMissing == nil {
				assert.NotContains(t, rec.Body.String(), "missing",
					"with nothing missing the body is the one every reader has always seen")
			}
		})
	}
}
