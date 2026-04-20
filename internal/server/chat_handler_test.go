package server

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	grpcodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// mockChatExecutor implements executor.ChatExecutor for handler tests.
type mockChatExecutor struct {
	sendFunc func(ctx context.Context, agentID string, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error)
}

func (m *mockChatExecutor) SendChatMessage(ctx context.Context, agentID string, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
	return m.sendFunc(ctx, agentID, req)
}

// testServerWithChat creates a Server with an injected ChatExecutor for testing.
func testServerWithChat(t *testing.T, chatExec executor.ChatExecutor) (*Server, *registry.InMemoryRegistry) {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger, WithChatExecutor(chatExec))
	require.NoError(t, err)
	return srv, reg
}

// registerTestAgent registers a healthy agent with a display name.
func registerTestAgent(t *testing.T, reg *registry.InMemoryRegistry, id, name string) {
	t.Helper()
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:      id,
		Name:    name,
		Address: "localhost:9090",
		Status:  registry.StatusHealthy,
	})
	require.NoError(t, err)
}

func TestHandleChat_Success(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, agentID string, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return &taskpb.ChatResponse{
				Reply:       "Hello from " + agentID,
				SessionId:   "sess-abc",
				AgentId:     agentID,
				Timestamp:   1713600000,
				ReplyStatus: "ok",
			}, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "ember-owl", "Ember Owl")

	body, _ := json.Marshal(chatRequest{
		Message: "Hi there!",
		UserID:  "local",
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/ember-owl/chat", body)
	assert.Equal(t, 200, rec.Code)

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "Hello from ember-owl", resp.Reply)
	assert.Equal(t, "sess-abc", resp.SessionID)
	assert.Equal(t, "ember-owl", resp.AgentID)
	assert.Equal(t, "Ember Owl", resp.AgentDisplayName)
	assert.Equal(t, "ok", resp.ReplyStatus)
	assert.Equal(t, int64(1713600000), resp.Timestamp)
}

func TestHandleChat_AgentDisplayNameFallsBackToID(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return &taskpb.ChatResponse{Reply: "hi", ReplyStatus: "ok"}, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	// Register agent with empty name.
	registerTestAgent(t, reg, "no-name", "")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/no-name/chat", body)
	assert.Equal(t, 200, rec.Code)

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "no-name", resp.AgentDisplayName)
}

func TestHandleChat_EmptyMessage(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "test-agent", "Test Agent")

	body, _ := json.Marshal(chatRequest{Message: ""})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/test-agent/chat", body)
	assert.Equal(t, 400, rec.Code)
	assert.Contains(t, rec.Body.String(), "message is required")
}

func TestHandleChat_OversizedMessage(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "test-agent", "Test Agent")

	body, _ := json.Marshal(chatRequest{Message: strings.Repeat("x", chatMaxMessageLength+1)})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/test-agent/chat", body)
	assert.Equal(t, 400, rec.Code)
	assert.Contains(t, rec.Body.String(), "exceeds maximum length")
}

func TestHandleChat_AgentNotFound(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, nil
		},
	}
	srv, _ := testServerWithChat(t, chatExec)

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/unknown-agent/chat", body)
	assert.Equal(t, 404, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent not found")
}

func TestHandleChat_GRPCInternalError(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, status.Error(grpcodes.Internal, "agent crashed")
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "crash-agent", "Crash Agent")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/crash-agent/chat", body)
	assert.Equal(t, 503, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent internal error")
}

func TestHandleChat_GRPCDeadlineExceeded(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, status.Error(grpcodes.DeadlineExceeded, "timeout")
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "slow-agent", "Slow Agent")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/slow-agent/chat", body)
	assert.Equal(t, 504, rec.Code)
	assert.Contains(t, rec.Body.String(), "did not respond in time")
}

func TestHandleChat_GRPCUnavailable(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, status.Error(grpcodes.Unavailable, "agent down")
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "down-agent", "Down Agent")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/down-agent/chat", body)
	assert.Equal(t, 503, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent unavailable")
}

func TestHandleChat_AgentNotReady(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, executor.ErrAgentNotReady
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "sick-agent", "Sick Agent")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/sick-agent/chat", body)
	assert.Equal(t, 503, rec.Code)
	assert.Contains(t, rec.Body.String(), "not healthy")
}

func TestHandleChat_EmptyReplyStatus(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return &taskpb.ChatResponse{
				Reply:       "",
				ReplyStatus: "empty",
				SessionId:   "sess-def",
			}, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "quiet-agent", "Quiet Agent")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/quiet-agent/chat", body)
	assert.Equal(t, 200, rec.Code)

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "", resp.Reply)
	assert.Equal(t, "empty", resp.ReplyStatus)
}

func TestHandleChat_WrongContentType(t *testing.T) {
	srv, _ := testServerWithChat(t, &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, nil
		},
	})

	req := httptest.NewRequest("POST", "/api/v1/agents/test-agent/chat", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "text/plain")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, 400, rec.Code)
	assert.Contains(t, rec.Body.String(), "Content-Type must be application/json")
}

func TestHandleChat_NoChatExecutor(t *testing.T) {
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	// Create server without chat executor.
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger)
	require.NoError(t, err)

	registerTestAgent(t, reg, "test-agent", "Test Agent")

	body, _ := json.Marshal(chatRequest{Message: "hello"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/test-agent/chat", body)
	assert.Equal(t, 500, rec.Code)
	assert.Contains(t, rec.Body.String(), "chat not available")
}

func TestHandleChat_PassesFieldsToGRPC(t *testing.T) {
	var captured *taskpb.ChatRequest
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, req *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			captured = req
			return &taskpb.ChatResponse{ReplyStatus: "ok"}, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "test-agent", "Test Agent")

	body, _ := json.Marshal(chatRequest{
		Message:         "hello",
		UserID:          "alice-01",
		SessionID:       "sess-xyz",
		TimeoutSeconds:  15,
		ParticipantType: "user",
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/test-agent/chat", body)
	assert.Equal(t, 200, rec.Code)

	require.NotNil(t, captured)
	assert.Equal(t, "test-agent", captured.AgentId)
	assert.Equal(t, "alice-01", captured.UserId)
	assert.Equal(t, "hello", captured.Message)
	assert.Equal(t, "sess-xyz", captured.SessionId)
	assert.Equal(t, int32(15), captured.TimeoutSeconds)
	assert.Equal(t, "user", captured.ParticipantType)
}

// TestHandleChat_InvalidAgentIDFormat verifies that agent IDs not matching
// resourceIDRegex (^[a-z0-9]([a-z0-9-]*[a-z0-9])?$) are rejected at the
// handler boundary, consistent with handleGetAgent/handleDeleteAgent.
// (PR #123 review finding F-01)
func TestHandleChat_InvalidAgentIDFormat(t *testing.T) {
	srv, _ := testServerWithChat(t, &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return nil, nil
		},
	})

	tests := []struct {
		name    string
		agentID string
	}{
		{"uppercase", "Agent-One"},
		{"underscore", "agent_one"},
		{"leading hyphen", "-bad-id"},
		{"trailing hyphen", "bad-id-"},
		{"dot separated", "agent.v2"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body, _ := json.Marshal(chatRequest{Message: "hello"})
			rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/"+tt.agentID+"/chat", body)
			assert.Equal(t, 400, rec.Code)
			assert.Contains(t, rec.Body.String(), "invalid agent ID format")
		})
	}
}

// TestHandleChat_MultiByteLengthLimit verifies the message length check counts
// Unicode characters (runes), not bytes. A 4000-character CJK string is ~12000
// bytes but should pass the 4000-character limit. (PR #123 review finding F-02)
func TestHandleChat_MultiByteLengthLimit(t *testing.T) {
	chatExec := &mockChatExecutor{
		sendFunc: func(_ context.Context, _ string, _ *taskpb.ChatRequest) (*taskpb.ChatResponse, error) {
			return &taskpb.ChatResponse{ReplyStatus: "ok"}, nil
		},
	}
	srv, reg := testServerWithChat(t, chatExec)
	registerTestAgent(t, reg, "test-agent", "Test Agent")

	// 4000 multi-byte characters (3 bytes each = 12000 bytes).
	// Should pass: exactly at the character limit.
	msg := strings.Repeat("あ", chatMaxMessageLength)
	body, _ := json.Marshal(chatRequest{Message: msg})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/test-agent/chat", body)
	assert.Equal(t, 200, rec.Code, "4000 multi-byte chars should be accepted")

	// 4001 multi-byte characters — should be rejected.
	msg2 := strings.Repeat("あ", chatMaxMessageLength+1)
	body2, _ := json.Marshal(chatRequest{Message: msg2})
	rec2 := doRequest(srv.Handler(), "POST", "/api/v1/agents/test-agent/chat", body2)
	assert.Equal(t, 400, rec2.Code, "4001 multi-byte chars should be rejected")
}
