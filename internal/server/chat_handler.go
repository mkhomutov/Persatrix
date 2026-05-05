package server

import (
	"errors"
	"net/http"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// chatMaxMessageLength is the maximum allowed message length in characters.
// (PR #123 review finding F-04: removed misleading configurability claim —
// this is a compile-time constant; no WithChatMaxMessageLength option exists.)
const chatMaxMessageLength = 4000

// chatDefaultTimeout / chatMinTimeout / chatMaxTimeout bound the time the
// chat handler waits for the agent's reply via
// [channels.ChannelRouter.PublishAndAwait]. The clamp matches the
// pre-rewrite gRPC chat timeout window so existing REST clients see no
// behavioural change in the success / timeout envelope (RFC 0011
// PR 4a-ii-β-2 amendment §"chat surface shape").
const (
	chatDefaultTimeout = 30 * time.Second
	chatMinTimeout     = time.Second
	chatMaxTimeout     = 300 * time.Second
)

var chatHandlerTracer = otel.Tracer("persatrix/server/chat")

// handleChat handles POST /api/v1/agents/{id}/chat as a synchronous-reply
// façade over the channels subsystem (RFC 0011 PR 4a-ii-β-2 — chat-as-DM
// unification per the [RFC 0011 amendment]).
//
// Flow per the amendment §"The unified model":
//
//  1. Validate request shape.
//  2. Resolve the canonical DM channel via
//     [channels.ChannelStore.GetOrCreateDM] (`user_id`, `agent_id`).
//     `GetOrCreateDM` is the access-control checkpoint — DM-membership
//     gating is the authority for user→agent addressing because the
//     per-publish response gate is implicitly bypassed on DM channels.
//  3. Build a `ChannelMessage` (sender_id=user_id, mentions=[agent_id])
//     and call [channels.ChannelRouter.PublishAndAwait], which:
//     - persists the inbound message,
//     - fans out via gRPC `ReceiveChannelMessage` to the agent,
//     - blocks on the in-process waiter until the agent's
//     `SEND_CHANNEL_MESSAGE` arrives via the REST publish path
//     (see `agents/action_executor.py::_handle_send_channel_message`).
//  4. Render the reply as `chatResponse`.
//
// Pre-amendment behaviour (gRPC `SendChatMessage` round-trip via
// [executor.ChatExecutor]) is removed; the wiring stays in
// `cmd/orchestrator/main.go` for now so external callers that only
// upgrade the orchestrator binary do not break, but `chatExecutor` is
// no longer consulted.
//
// TODO(security): per RFC 0011 amendment §"Security note", DM creation
// is the access-control checkpoint. v0.3.0 ships with no auth check
// (matches pre-amendment chat behaviour); the per-agent ACL slated for
// RFC 0009 Phase 4 plugs in here.
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
	if utf8.RuneCountInString(req.Message) > chatMaxMessageLength {
		writeError(w, "BAD_REQUEST", "message exceeds maximum length of 4000 characters", http.StatusBadRequest)
		return
	}

	// `user_id` is required in the channel-routed model: it becomes the
	// DM peer (and the message sender). The pre-rewrite path tolerated an
	// empty user_id by populating sender_id=nil; under chat-as-DM there is
	// no canonical DM peer without it. Default to "local" to preserve the
	// `persatrix chat` REPL behaviour where no auth is configured.
	userID := req.UserID
	if userID == "" {
		userID = "local"
	}

	if s.channelStore == nil || s.channelRouter == nil {
		s.logger.Error("chat: channels subsystem not configured")
		writeError(w, "INTERNAL", "chat not available", http.StatusInternalServerError)
		return
	}

	// Look up agent in registry — 404 if missing, 503 if not healthy.
	// The channels publish path also performs per-recipient health
	// checks via [channels.GRPCMessageDispatcher], so this is
	// defense-in-depth that lets the handler short-circuit before
	// opening a DM row in the store.
	agent, err := s.registry.Get(ctx, agentID)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			writeError(w, "NOT_FOUND", "agent not found", http.StatusNotFound)
			return
		}
		s.logger.Error("chat: registry lookup failed", zap.String("agent_id", agentID), zap.Error(err))
		writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		return
	}
	if agent.Status != registry.StatusHealthy {
		writeError(w, "UNAVAILABLE", "agent is not healthy", http.StatusServiceUnavailable)
		return
	}

	dm, err := s.channelStore.GetOrCreateDM(ctx, userID, agentID)
	if err != nil {
		// `GetOrCreateDM` rejects same-id, colon, whitespace, empty
		// participant ids. Treat all as 400 — caller-supplied id
		// hygiene problems, not server faults.
		s.logger.Warn("chat: GetOrCreateDM rejected request",
			zap.String("user_id", userID), zap.String("agent_id", agentID), zap.Error(err),
		)
		writeError(w, "BAD_REQUEST", "invalid participant id", http.StatusBadRequest)
		return
	}

	timeout := chatDefaultTimeout
	if req.TimeoutSeconds > 0 {
		timeout = time.Duration(req.TimeoutSeconds) * time.Second
		if timeout < chatMinTimeout {
			timeout = chatMinTimeout
		} else if timeout > chatMaxTimeout {
			timeout = chatMaxTimeout
		}
	}

	sessionID := req.SessionID
	if sessionID == "" {
		sessionID = uuid.NewString()
	}

	inbound := channels.ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: dm.ID,
		SenderID:  userID,
		Content:   req.Message,
		Mentions:  []string{agentID},
		Timestamp: time.Now().UTC(),
	}

	reply, err := s.channelRouter.PublishAndAwait(ctx, inbound, agentID, timeout)
	if err != nil {
		switch {
		case errors.Is(err, channels.ErrChatTimeout):
			writeError(w, "DEADLINE_EXCEEDED", "agent did not respond in time", http.StatusGatewayTimeout)
		case errors.Is(err, channels.ErrNotMember):
			// Should be unreachable — `GetOrCreateDM` adds both
			// participants — but surface as 5xx if it fires; would
			// indicate store corruption.
			s.logger.Error("chat: ErrNotMember on freshly-created DM", zap.String("dm", dm.ID))
			writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		case errors.Is(err, channels.ErrInvalidChannelType):
			s.logger.Error("chat: ErrInvalidChannelType on DM publish", zap.String("dm", dm.ID), zap.Error(err))
			writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		default:
			s.logger.Error("chat: PublishAndAwait failed",
				zap.String("agent_id", agentID), zap.String("user_id", userID), zap.Error(err))
			writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		}
		return
	}

	displayName := agent.Name
	if displayName == "" {
		displayName = agentID
	}

	resp := chatResponse{
		Reply:            reply.Content,
		SessionID:        sessionID,
		AgentID:          agentID,
		Timestamp:        reply.Timestamp.Unix(),
		AgentDisplayName: displayName,
		ReplyStatus:      "ok",
	}

	writeJSON(w, resp, http.StatusOK)
}
