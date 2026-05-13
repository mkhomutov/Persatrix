package server

import (
	"context"
	"errors"
	"net/http"
	"sync"
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
// dashboards and alert rules.
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

// chatLocalFallbackWarnOnce guards the once-per-process Warn emitted
// by [Server.handleChat] when a chat request omits `user_id` and
// falls back to the shared `"local"` pseudo-user. The hazard the Warn
// signposts (cross-talk via a shared `dm:<agent>:local` channel) is
// structural, not per-request — gating the line behind sync.Once
// prevents the warn from flooding logs at chat QPS while preserving
// the visibility intent. Mirrors the `channelFallbackWarnOnce`
// precedent established by PR #245 review round 3 Should-Fix #3 and
// hardened for test isolation in ISSUE-0009 / PR #246.
var chatLocalFallbackWarnOnce sync.Once

// TestingResetChatLocalFallbackWarnOnce resets the package-level
// guard to its zero state. Exported for use in test setup only;
// production code must never call it. Direct assignment from test
// files is a data race when any sibling test runs with t.Parallel()
// (see ISSUE-0009 for the full rationale on why a helper, not direct
// assignment, is the safe pattern).
func TestingResetChatLocalFallbackWarnOnce() {
	chatLocalFallbackWarnOnce = sync.Once{}
}

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
	// WARNING: the `"local"` fallback is a SHARED
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
		chatLocalFallbackWarnOnce.Do(func() {
			s.logger.Warn("chat: empty user_id; using shared 'local' fallback (cross-talk hazard, RFC 0009 Phase 4)",
				zap.String("agent_id", agentID),
			)
		})
	}

	// Attach `agent.id` and `user.id` to the span now that both are
	// resolved. Without these, traces of chat failures cannot be
	// filtered per-agent or per-user — the very dimensions an
	// operator reaches for first when triaging a chat regression.
	// `agent.id` is the validated path value; `user.id` reflects the
	// post-fallback value (`"local"` when omitted) so the span
	// matches the log line emitted above.
	span.SetAttributes(
		attribute.String("agent.id", agentID),
		attribute.String("user.id", userID),
	)

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
	// ISSUE-0034: demote the user's membership to RespondNever so
	// `ChannelRouter.fanout` skips dispatch to the user on every agent
	// reply. The user is the chat caller — they read replies via the
	// in-process `replyWaiter`, not via gRPC push, and they are not in
	// the agent registry. Without this normalisation the dispatcher
	// emits a `level=warn`
	// "channels: dispatch target not registered" line per chat reply.
	// Idempotent: repeats on every chat turn (the row stays at `never`
	// after the first call). Runs only on a clean `GetOrCreateDM` so
	// the existing error discrimination below remains the single 4xx /
	// 5xx mapping point.
	if err == nil {
		if pErr := s.channelStore.SetMemberPolicy(ctx, dm.ID, userID, channels.RespondNever); pErr != nil {
			s.logger.Warn("chat: SetMemberPolicy(user, never) failed; reply fanout will produce per-reply WARN until next chat turn",
				zap.String("dm", dm.ID),
				zap.String("user_id", userID),
				zap.Error(pErr),
			)
		}
	}
	if err != nil {
		// Discriminate validation errors from server-side I/O errors.
		// Pre-fix behaviour mapped EVERY
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

	sessionID := req.ChatSessionID
	if sessionID == "" {
		sessionID = uuid.NewString()
	}

	// Propagate `chat_session_id` and `participant_type` into
	// ChannelMessage.Metadata so the wire
	// fields are not silently inert. RFC 0011 amendment §Mapping
	// retains `metadata["participant_type"]`; the chat-session token
	// rides on `metadata["chat_session_id"]` (renamed from
	// `"session_id"` in v0.3.1 to disambiguate from RFC 0031's
	// operator-namespace session id — RFC 0031 OQ #8).
	metadata := map[string]any{
		"chat_session_id": sessionID,
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
			// The DM was deleted between `GetOrCreateDM` and
			// `PublishAndAwait`'s inner `Publish`
			// (e.g. via `DELETE /api/v1/channels/{id}` from another
			// caller). 404 is the correct semantic — the resource
			// addressed by the publish disappeared — and gives the
			// caller an actionable retry signal rather than an opaque
			// 500.
			writeError(w, "NOT_FOUND", "chat channel disappeared", http.StatusNotFound)
		case errors.Is(err, channels.ErrWaiterAlreadyRegistered):
			// Another chat for the same `(user, agent)` DM is already
			// in flight. The
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
			// Client disconnected before the agent replied. This is
			// not a server fault;
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

	// Defensive guard against a reply whose Timestamp was never
	// stamped by the publisher. `time.Time{}.Unix()`
	// returns -62135596800 (year 1754); substitute the current wall
	// clock so the client never observes a junk negative epoch second.
	replyTimestamp := reply.Timestamp
	if replyTimestamp.IsZero() {
		replyTimestamp = time.Now().UTC()
	}

	resp := chatResponse{
		Reply:            reply.Content,
		ChatSessionID:    sessionID,
		AgentID:          agentID,
		Timestamp:        replyTimestamp.Unix(),
		AgentDisplayName: displayName,
		ReplyStatus:      "ok",
	}

	writeJSON(w, resp, http.StatusOK)
}
