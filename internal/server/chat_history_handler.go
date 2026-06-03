package server

import (
	"errors"
	"net/http"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// handleGetChatHistory handles GET /api/v1/agents/{id}/chat/history?user_id=&limit=&before=
//
// RFC 0048 amendment §B — read-only conversation continuity. The chat DM is a
// persisted channel, but its id is server-normalised (GetOrCreateDM); the client
// must not reconstruct it. This endpoint resolves the canonical DM for
// (user_id, agent_id) via the non-mutating LookupDM and returns its history in
// the same `historyResponse` shape as GET /api/v1/channels/{id}/messages, so the
// Chat panel can seed its transcript on reload and continue appending live turns.
//
// A persona never chatted with is the expected fresh-start case → 200 with an
// empty messages array, NOT 404 (Decision #2). The endpoint is read-only and
// creates nothing: opening the history of a never-used persona must not
// materialise an empty DM.
//
// TODO(v0.2): `user_id` is an unauthenticated *lookup key*, not an authorization
// boundary. Like the chat POST above it (see the rate-limiting TODO in
// registerRoutes), this endpoint trusts the query principal — so any caller can
// read any user's conversation by supplying their id. The per-user scoping the
// tests assert is isolation-by-key, not access control; a read that returns full
// conversation history is a larger exposure than the write-only POST and needs a
// real principal/auth check before this is anything but a single-tenant console.
func (s *Server) handleGetChatHistory(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
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

	// user_id is required — it is half the DM key. Unlike the chat POST (which
	// defaults an omitted user to the shared "local" fallback), a read endpoint
	// has no turn to attribute, so an absent principal is a client error rather
	// than a silent fallback that would leak the shared-local history.
	userID := r.URL.Query().Get("user_id")
	if userID == "" {
		writeError(w, "BAD_REQUEST", "user_id is required", http.StatusBadRequest)
		return
	}

	// limit/before mirror the channel-history query params verbatim (same
	// parsing, same validation), so keyset pagination is available for free and
	// a malformed value errors loudly the same way it does on /channels.
	limit, err := parseLimit(r, channelDefaultHistoryLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	before, err := parseBefore(r)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	dm, err := s.channelStore.LookupDM(r.Context(), userID, agentID)
	if err != nil {
		switch {
		case errors.Is(err, channels.ErrChannelNotFound):
			// Fresh start — no conversation yet. Return the same empty envelope
			// the timeline would render for an empty channel, so the client's
			// history-seed path has nothing special to branch on.
			writeJSON(w, historyResponse{Messages: messagesToResponse(nil)}, http.StatusOK)
			return
		case errors.Is(err, channels.ErrInvalidParticipantID):
			// e.g. user_id == agent_id, or an otherwise ill-formed pair — the
			// same validation GetOrCreateDM applies, surfaced as a 400.
			writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
			return
		default:
			s.logger.Error("chat: history lookup failed",
				zap.String("agent_id", agentID), zap.Error(err))
			writeError(w, "INTERNAL", "failed to load chat history", http.StatusInternalServerError)
			return
		}
	}

	msgs, err := s.channelStore.GetHistory(r.Context(), dm.ID, limit, before)
	if err != nil {
		s.logger.Error("chat: history failed",
			zap.String("agent_id", agentID),
			zap.String("channel_id", dm.ID), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load chat history", http.StatusInternalServerError)
		return
	}
	writeJSON(w, historyResponse{Messages: messagesToResponse(msgs)}, http.StatusOK)
}
