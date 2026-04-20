package server

import (
	"errors"
	"net/http"
	"unicode/utf8"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
	grpcodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// chatMaxMessageLength is the maximum allowed message length in characters.
// (PR #123 review finding F-04: removed misleading configurability claim —
// this is a compile-time constant; no WithChatMaxMessageLength option exists.)
const chatMaxMessageLength = 4000

var chatHandlerTracer = otel.Tracer("persatrix/server/chat")

// handleChat handles POST /api/v1/agents/{id}/chat requests.
// It validates the request, dispatches a gRPC SendChatMessage call to the agent,
// populates agent_display_name from the registry, and maps gRPC errors to HTTP status codes.
func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	ctx, span := chatHandlerTracer.Start(r.Context(), "http.chat",
		trace.WithAttributes(attribute.String("http.route", "/api/v1/agents/{id}/chat")),
	)
	defer span.End()
	r = r.WithContext(ctx)

	if !requireJSON(w, r) {
		return
	}

	agentID := r.PathValue("id")
	if agentID == "" {
		writeError(w, "BAD_REQUEST", "agent_id is required", http.StatusBadRequest)
		return
	}
	// Validate agent ID format — defense-in-depth consistent with handleGetAgent
	// and handleDeleteAgent. Prevents arbitrary strings (path traversal, injection
	// chars) from reaching the registry layer. (PR #123 review finding F-01)
	if !resourceIDRegex.MatchString(agentID) {
		writeError(w, "BAD_REQUEST", "invalid agent ID format", http.StatusBadRequest)
		return
	}

	var req chatRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	if req.Message == "" {
		writeError(w, "BAD_REQUEST", "message is required", http.StatusBadRequest)
		return
	}

	// Use RuneCountInString to count characters, not bytes, so multi-byte
	// UTF-8 text (emoji, CJK) is measured consistently with the documented
	// "4000 characters" limit. (PR #123 review finding F-02)
	if utf8.RuneCountInString(req.Message) > chatMaxMessageLength {
		writeError(w, "BAD_REQUEST", "message exceeds maximum length of 4000 characters", http.StatusBadRequest)
		return
	}

	if s.chatExecutor == nil {
		s.logger.Error("chat executor not configured")
		writeError(w, "INTERNAL", "chat not available", http.StatusInternalServerError)
		return
	}

	// Look up agent in registry for display name and to verify it exists.
	// NOTE: The executor performs a second registry lookup to check health status
	// and retrieve the gRPC address. This intentional duplication serves as
	// defense-in-depth — the handler can short-circuit with 404 before touching
	// the executor, while the executor verifies health at call time. Acceptable
	// overhead for v0.1 in-memory registry; consolidate for v0.2 SQLite migration.
	// (PR #123 review finding S-01)
	agent, err := s.registry.Get(ctx, agentID)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			writeError(w, "NOT_FOUND", "agent not found", http.StatusNotFound)
			return
		}
		s.logger.Error("registry lookup failed", zap.String("agent_id", agentID), zap.Error(err))
		writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		return
	}

	// Build gRPC request.
	grpcReq := &taskpb.ChatRequest{
		AgentId:         agentID,
		UserId:          req.UserID,
		Message:         req.Message,
		SessionId:       req.SessionID,
		TimeoutSeconds:  req.TimeoutSeconds,
		ParticipantType: req.ParticipantType,
	}

	grpcResp, err := s.chatExecutor.SendChatMessage(ctx, agentID, grpcReq)
	if err != nil {
		s.logger.Error("SendChatMessage failed",
			zap.String("agent_id", agentID),
			zap.Error(err),
		)

		// Map gRPC errors to HTTP status codes.
		if st, ok := status.FromError(err); ok {
			switch st.Code() {
			case grpcodes.DeadlineExceeded:
				writeError(w, "DEADLINE_EXCEEDED", "agent did not respond in time", http.StatusGatewayTimeout)
				return
			case grpcodes.Internal:
				// gRPC Internal from agent → HTTP 503 (agent-side failure, not orchestrator)
				writeError(w, "INTERNAL", "agent internal error", http.StatusServiceUnavailable)
				return
			case grpcodes.Unavailable:
				writeError(w, "UNAVAILABLE", "agent unavailable", http.StatusServiceUnavailable)
				return
			}
		}

		// Check for executor sentinel errors.
		if errors.Is(err, executor.ErrAgentNotReady) {
			writeError(w, "UNAVAILABLE", "agent is not healthy", http.StatusServiceUnavailable)
			return
		}

		writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		return
	}

	// Populate agent_display_name from registry. Fall back to agent_id if empty.
	displayName := agent.Name
	if displayName == "" {
		displayName = agentID
	}

	resp := chatResponse{
		Reply:            grpcResp.GetReply(),
		SessionID:        grpcResp.GetSessionId(),
		AgentID:          grpcResp.GetAgentId(),
		Timestamp:        grpcResp.GetTimestamp(),
		AgentDisplayName: displayName,
		ReplyStatus:      grpcResp.GetReplyStatus(),
	}

	writeJSON(w, resp, http.StatusOK)
}
