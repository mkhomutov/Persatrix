package server

import (
	"context"
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

// statusClientClosedRequest is the non-standard 499 status code used
// (originally by nginx) to signal that the client closed the
// connection before the server finished processing. The Go stdlib
// has no constant for it. We emit it from the chat handler when
// `PublishAndAwait` returns `context.Canceled` so a client
// disconnect mid-flight is not conflated with a 5xx server fault in
// dashboards and alert rules. PR #251 review "Should fix #1".
const statusClientClosedRequest = 499

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
	//
	// WARNING (PR #251 review): the `"local"` fallback is a SHARED
	// pseudo-user. In any deployment where >1 unauthenticated caller can
	// hit this endpoint, all such callers transparently share the same
	// canonical DM (`dm:<agent>:local`) and therefore the same persisted
	// chat history. This is acceptable only for single-user development
	// (the `persatrix chat` REPL); the v0.3.0 release notes call this
	// out as a known limitation. The proper fix lands with RFC 0009
	// Phase 4 auth — see TODO(security) above. Until then we log a
	// per-request warning so the cross-talk hazard is visible in logs.
	userID := req.UserID
	if userID == "" {
		userID = "local"
		s.logger.Warn("chat: empty user_id; using shared 'local' fallback (cross-talk hazard, RFC 0009 Phase 4)",
			zap.String("agent_id", agentID),
		)
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
		// PR #251 deep-review M-1: discriminate validation errors
		// from server-side I/O errors. Pre-fix behaviour mapped EVERY
		// `GetOrCreateDM` error to 400 BAD_REQUEST, which silently
		// converted transient SQLite failures (lock contention,
		// disk-full, closed handle, BeginTx/Commit faults) into a
		// "fix your input" envelope — wrong actionable signal,
		// poisoned 4xx/5xx ratios, hidden outages.
		//
		// Discrimination rules (in order):
		//
		//  1. `context.Canceled` — caller went away mid-call. Same
		//     499 mapping the `PublishAndAwait` branch uses below;
		//     keeps the client-disconnect classification consistent
		//     across all I/O steps in the handler.
		//  2. `ErrInvalidParticipantID` — caller-supplied id-hygiene
		//     problem (empty / colon / whitespace / same-as-agent).
		//     400 BAD_REQUEST is correct.
		//  3. anything else — server-side fault. 500 INTERNAL,
		//     log at Error so the line shows up in dashboards.
		switch {
		case errors.Is(err, context.Canceled):
			s.logger.Info("chat: client cancelled before DM resolve",
				zap.String("agent_id", agentID), zap.String("user_id", userID))
			writeError(w, "CLIENT_CLOSED_REQUEST", "client closed request", statusClientClosedRequest)
		case errors.Is(err, channels.ErrInvalidParticipantID):
			s.logger.Warn("chat: GetOrCreateDM rejected request",
				zap.String("user_id", userID), zap.String("agent_id", agentID), zap.Error(err),
			)
			writeError(w, "BAD_REQUEST", "invalid participant id", http.StatusBadRequest)
		default:
			s.logger.Error("chat: GetOrCreateDM failed",
				zap.String("user_id", userID), zap.String("agent_id", agentID), zap.Error(err),
			)
			writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
		}
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

	// PR #251 review (M-2/M-3): propagate `session_id` and
	// `participant_type` into ChannelMessage.Metadata so the wire
	// fields are not silently inert. RFC 0011 amendment §Mapping
	// retains `metadata["participant_type"]`; we use the same key
	// for `session_id` to keep one conversation-segmentation
	// vocabulary across the chat and channels surfaces.
	metadata := map[string]any{
		"session_id": sessionID,
	}
	if req.ParticipantType != "" {
		metadata["participant_type"] = req.ParticipantType
	}

	inbound := channels.ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: dm.ID,
		SenderID:  userID,
		Content:   req.Message,
		Mentions:  []string{agentID},
		Timestamp: time.Now().UTC(),
		Metadata:  metadata,
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
		case errors.Is(err, channels.ErrChannelNotFound):
			// PR #251 review M-3: the DM was deleted between
			// `GetOrCreateDM` and `PublishAndAwait`'s inner `Publish`
			// (e.g. via `DELETE /api/v1/channels/{id}` from another
			// caller). 404 is the correct semantic — the resource
			// addressed by the publish disappeared — and gives the
			// caller an actionable retry signal rather than an opaque
			// 500.
			writeError(w, "NOT_FOUND", "chat channel disappeared", http.StatusNotFound)
		case errors.Is(err, channels.ErrWaiterAlreadyRegistered):
			// PR #251 review "Should fix #2": another chat for the
			// same `(user, agent)` DM is already in flight. The
			// `replyWaiter` keys on `(channelID, awaitFromAgentID)`,
			// so two concurrent chat requests for the same DM
			// collide deterministically. 409 Conflict is the
			// canonical surface for "your request is well-formed but
			// the resource state forbids it right now"; clients can
			// safely retry once the prior turn completes.
			s.logger.Info("chat: concurrent chat on same DM rejected",
				zap.String("agent_id", agentID), zap.String("user_id", userID))
			writeError(w, "CONFLICT", "another chat is in flight for this DM; retry shortly", http.StatusConflict)
		case errors.Is(err, context.Canceled):
			// PR #251 review "Should fix #1": client disconnected
			// before the agent replied. This is not a server fault;
			// log at Info (not Error) so it does not poison alert
			// rules built on the 5xx error log line, and emit the
			// nginx-style 499 status. The response bytes likely
			// never reach the caller — they have already gone — but
			// `httptest` recorders still capture them, which is what
			// makes this path testable.
			s.logger.Info("chat: client cancelled request before reply",
				zap.String("agent_id", agentID), zap.String("user_id", userID))
			writeError(w, "CLIENT_CLOSED_REQUEST", "client closed request", statusClientClosedRequest)
		case errors.Is(err, context.DeadlineExceeded):
			// Caller's own deadline (e.g. an upstream proxy timeout)
			// fired before our `chatMaxTimeout` clamp. Same envelope
			// as `ErrChatTimeout` — the user observation is
			// indistinguishable.
			writeError(w, "DEADLINE_EXCEEDED", "request deadline exceeded", http.StatusGatewayTimeout)
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

	// PR #251 review (L-1): defensive guard against a reply whose
	// Timestamp was never stamped by the publisher. `time.Time{}.Unix()`
	// returns -62135596800 (year 1754); substitute the current wall
	// clock so the client never observes a junk negative epoch second.
	replyTimestamp := reply.Timestamp
	if replyTimestamp.IsZero() {
		replyTimestamp = time.Now().UTC()
	}

	resp := chatResponse{
		Reply:            reply.Content,
		SessionID:        sessionID,
		AgentID:          agentID,
		Timestamp:        replyTimestamp.Unix(),
		AgentDisplayName: displayName,
		ReplyStatus:      "ok",
	}

	writeJSON(w, resp, http.StatusOK)
}
