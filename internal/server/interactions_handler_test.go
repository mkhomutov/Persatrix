package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// fakeInteractionReader is a test double for executor.InteractionReader.
type fakeInteractionReader struct {
	resp     *taskpb.ClosedInteractionsResponse
	err      error
	gotReq   *taskpb.ClosedInteractionsRequest
	gotAgent string
}

func (f *fakeInteractionReader) GetClosedInteractions(_ context.Context, agentID string, req *taskpb.ClosedInteractionsRequest) (*taskpb.ClosedInteractionsResponse, error) {
	f.gotAgent = agentID
	f.gotReq = req
	return f.resp, f.err
}

func interactionTestServer(t *testing.T, reader executor.InteractionReader) *Server {
	t.Helper()
	logger := zap.NewNop()
	opts := []ServerOption{}
	if reader != nil {
		opts = append(opts, WithInteractionReader(reader))
	}
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		opts...,
	)
	require.NoError(t, err)
	return srv
}

func TestHandleGetClosedInteractions_ProjectsResponse(t *testing.T) {
	reader := &fakeInteractionReader{resp: &taskpb.ClosedInteractionsResponse{
		Interactions: []*taskpb.ClosedInteraction{{
			InteractionId: "i-1",
			Scope:         "group:room-7",
			StartedAt:     10.0,
			ClosedAt:      20.0,
			TurnCount:     5,
			CloseReason:   "cost",
			Summary:       "converged on Thursday",
		}},
	}}
	srv := interactionTestServer(t, reader)

	req := httptest.NewRequest("GET", "/api/v1/agents/agent-x/interactions/closed?limit=5&scope=group:room-7", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	require.Equal(t, 200, rec.Code)
	var body closedInteractionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))
	require.Len(t, body.Interactions, 1)
	it := body.Interactions[0]
	assert.Equal(t, "i-1", it.InteractionID)
	assert.Equal(t, "group:room-7", it.Scope)
	assert.Equal(t, "cost", it.CloseReason)
	assert.Equal(t, "converged on Thursday", it.Summary)
	assert.Equal(t, int32(5), it.TurnCount)
	assert.Equal(t, 20.0, it.ClosedAt)

	// Query params are threaded onto the gRPC request.
	assert.Equal(t, "agent-x", reader.gotAgent)
	assert.Equal(t, "group:room-7", reader.gotReq.GetScope())
	assert.Equal(t, int32(5), reader.gotReq.GetLimit())
}

func TestHandleGetClosedInteractions_AgentNotFound(t *testing.T) {
	reader := &fakeInteractionReader{err: registry.ErrAgentNotFound}
	srv := interactionTestServer(t, reader)

	req := httptest.NewRequest("GET", "/api/v1/agents/ghost/interactions/closed", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	assert.Equal(t, 404, rec.Code)
}

func TestHandleGetClosedInteractions_AgentNotReady(t *testing.T) {
	reader := &fakeInteractionReader{err: executor.ErrAgentNotReady}
	srv := interactionTestServer(t, reader)

	req := httptest.NewRequest("GET", "/api/v1/agents/agent-x/interactions/closed", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	assert.Equal(t, 503, rec.Code)
}

// The InteractionReader makes a live gRPC call to the agent, so its error
// is frequently a gRPC *status* error (not one of the executor's Go
// sentinels): the agent-side servicer returns NOT_FOUND for an id unknown
// to the agent process, and the transport returns Unavailable /
// DeadlineExceeded when the agent is down or slow between the registry
// health-read and the call. Those must map to 404 / 503, not a blanket 500.
func TestHandleGetClosedInteractions_GRPCStatusErrorMapping(t *testing.T) {
	cases := []struct {
		name     string
		err      error
		wantCode int
	}{
		{"grpc NotFound → 404", status.Error(codes.NotFound, "agent gone"), 404},
		{"grpc Unavailable → 503", status.Error(codes.Unavailable, "agent down"), 503},
		{"grpc DeadlineExceeded → 503", status.Error(codes.DeadlineExceeded, "slow"), 503},
		{"grpc InvalidArgument → 400", status.Error(codes.InvalidArgument, "bad"), 400},
		{"grpc Internal → 500", status.Error(codes.Internal, "boom"), 500},
		{"non-status error → 500", errors.New("plain"), 500},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reader := &fakeInteractionReader{err: tc.err}
			srv := interactionTestServer(t, reader)

			req := httptest.NewRequest("GET", "/api/v1/agents/agent-x/interactions/closed", nil)
			rec := httptest.NewRecorder()
			srv.Handler().ServeHTTP(rec, req)

			assert.Equal(t, tc.wantCode, rec.Code)
		})
	}
}

func TestHandleGetClosedInteractions_NotConfigured(t *testing.T) {
	srv := interactionTestServer(t, nil) // no reader wired

	req := httptest.NewRequest("GET", "/api/v1/agents/agent-x/interactions/closed", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	assert.Equal(t, 503, rec.Code)
}

func TestHandleGetClosedInteractions_InvalidAgentID(t *testing.T) {
	reader := &fakeInteractionReader{resp: &taskpb.ClosedInteractionsResponse{}}
	srv := interactionTestServer(t, reader)

	req := httptest.NewRequest("GET", "/api/v1/agents/Bad_ID!/interactions/closed", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	assert.Equal(t, 400, rec.Code)
	assert.Nil(t, reader.gotReq, "reader must not be called on a malformed agent id")
}
