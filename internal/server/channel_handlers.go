// Channel REST handlers — RFC 0011 §C, Phase 1b.
//
// This file ships the create/list/get/publish/history/thread/add-member
// endpoints. The two DELETE endpoints listed in the §C table are
// deferred to PR 4 alongside the response gate that needs to react to
// membership removal.
package server

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// channelDefaultListLimit and channelDefaultHistoryLimit mirror the §C
// "Query parameters" table defaults. Bounded above by `channelMaxLimit`
// so a hostile `?limit=999999` cannot allocate gigabytes of slice
// before the store query starts. The cap is generous (the global
// `max_channels` cap is 50 by default) and small enough that one page
// fits comfortably in a single TCP segment.
const (
	channelDefaultListLimit    = 100
	channelDefaultHistoryLimit = 50
	channelDefaultThreadLimit  = 100
	channelMaxLimit            = 1000
)

// handleCreateChannel handles POST /api/v1/channels.
//
// Only group channels are creatable here (RFC 0011 §C). The server
// derives the canonical ID (`group:<name>`) so callers cannot supply a
// drifted `(id, name)` pair — SF-2 of PR #231 review hardens this on
// the store side too, but rejecting at the boundary gives a clearer
// error.
func (s *Server) handleCreateChannel(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	var req createChannelRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Name == "" {
		writeError(w, "BAD_REQUEST", "name is required", http.StatusBadRequest)
		return
	}
	if len(req.Members) == 0 {
		writeError(w, "BAD_REQUEST", "at least one member is required", http.StatusBadRequest)
		return
	}

	canonicalID := "group:" + req.Name
	ch := channels.Channel{
		ID:          canonicalID,
		Name:        req.Name,
		Type:        channels.ChannelTypeGroup,
		Description: req.Description,
	}

	// PR #245 review (High): the previous implementation called
	// CreateChannel followed by an N-call AddMember loop with no
	// transaction. A failure mid-loop left an orphan channel that
	// poisoned the client's natural retry with 409 CONFLICT. The store's
	// CreateChannelWithMembers helper makes the bundle atomic so we no
	// longer need handler-side rollback. Member translation stays here
	// because the wire shape (channelMemberRequest) is server-local.
	members := make([]channels.Member, 0, len(req.Members))
	for _, m := range req.Members {
		policy := channels.RespondWhenMentioned
		if m.Respond != "" {
			policy = channels.RespondPolicy(m.Respond)
		}
		members = append(members, channels.Member{
			ParticipantID: m.ID,
			RespondPolicy: policy,
		})
	}
	if err := s.channelStore.CreateChannelWithMembers(r.Context(), ch, members); err != nil {
		s.writeChannelError(w, err)
		return
	}

	created, err := s.channelStore.GetChannel(r.Context(), canonicalID)
	if err != nil {
		s.logger.Error("channels: post-create lookup failed",
			zap.String("channel_id", canonicalID), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load created channel", http.StatusInternalServerError)
		return
	}
	resp := channelToResponse(created, nil)
	writeJSON(w, resp, http.StatusCreated)
}

// handleListChannels handles GET /api/v1/channels.
func (s *Server) handleListChannels(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	limit, err := parseLimit(r, channelDefaultListLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	chs, err := s.channelStore.ListChannels(r.Context())
	if err != nil {
		s.logger.Error("channels: list failed", zap.Error(err))
		writeError(w, "INTERNAL", "failed to list channels", http.StatusInternalServerError)
		return
	}
	if len(chs) > limit {
		chs = chs[:limit]
	}
	out := make([]channelResponse, 0, len(chs))
	for _, c := range chs {
		out = append(out, channelToResponse(c, nil))
	}
	writeJSON(w, listChannelsResponse{Channels: out}, http.StatusOK)
}

// handleGetChannel handles GET /api/v1/channels/{id}.
func (s *Server) handleGetChannel(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	ch, err := s.channelStore.GetChannel(r.Context(), id)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	members, err := s.channelStore.GetMembers(r.Context(), id)
	if err != nil {
		s.logger.Error("channels: get members failed",
			zap.String("channel_id", id), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load channel members", http.StatusInternalServerError)
		return
	}
	writeJSON(w, channelToResponse(ch, members), http.StatusOK)
}

// handlePublishMessage handles POST /api/v1/channels/{id}/messages.
//
// Routes through [ChannelRouter.Publish] when a router was injected, so
// channel_type cross-validation and fanout fire. Falls back to a direct
// store write when the router is unset (test fixtures only — production
// always wires the router).
func (s *Server) handlePublishMessage(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	id := r.PathValue("id")
	var req publishMessageRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.SenderID == "" {
		writeError(w, "BAD_REQUEST", "sender_id is required", http.StatusBadRequest)
		return
	}
	if req.Content == "" {
		writeError(w, "BAD_REQUEST", "content is required", http.StatusBadRequest)
		return
	}

	msg := channels.ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  req.SenderID,
		Content:   req.Content,
		Timestamp: time.Now().UTC(),
		ThreadID:  req.ThreadID,
		Mentions:  req.Mentions,
		Metadata:  req.Metadata,
	}

	var pubErr error
	if s.channelRouter != nil {
		pubErr = s.channelRouter.Publish(r.Context(), msg, req.ChannelType)
	} else {
		pubErr = s.channelStore.PublishMessage(r.Context(), msg)
	}
	if pubErr != nil {
		s.writeChannelError(w, pubErr)
		return
	}

	stored, err := s.channelStore.GetMessage(r.Context(), msg.ID)
	if err != nil {
		// The publish committed; lookup failure is internal-only.
		s.logger.Error("channels: post-publish lookup failed",
			zap.String("message_id", msg.ID), zap.Error(err))
		writeJSON(w, messageToResponse(msg), http.StatusCreated)
		return
	}
	writeJSON(w, messageToResponse(stored), http.StatusCreated)
}

// handleGetChannelHistory handles GET /api/v1/channels/{id}/messages.
func (s *Server) handleGetChannelHistory(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	if _, err := s.channelStore.GetChannel(r.Context(), id); err != nil {
		s.writeChannelError(w, err)
		return
	}
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
	msgs, err := s.channelStore.GetHistory(r.Context(), id, limit, before)
	if err != nil {
		s.logger.Error("channels: history failed",
			zap.String("channel_id", id), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load channel history", http.StatusInternalServerError)
		return
	}
	writeJSON(w, historyResponse{Messages: messagesToResponse(msgs)}, http.StatusOK)
}

// handleGetThread handles GET /api/v1/channels/{id}/messages/{msg_id}/thread.
func (s *Server) handleGetThread(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	chID := r.PathValue("id")
	msgID := r.PathValue("msg_id")
	if _, err := s.channelStore.GetChannel(r.Context(), chID); err != nil {
		s.writeChannelError(w, err)
		return
	}
	if _, err := s.channelStore.GetMessage(r.Context(), msgID); err != nil {
		s.writeChannelError(w, err)
		return
	}
	limit, err := parseLimit(r, channelDefaultThreadLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	msgs, err := s.channelStore.GetThread(r.Context(), msgID, limit)
	if err != nil {
		s.logger.Error("channels: thread failed",
			zap.String("channel_id", chID),
			zap.String("message_id", msgID),
			zap.Error(err))
		writeError(w, "INTERNAL", "failed to load thread", http.StatusInternalServerError)
		return
	}
	writeJSON(w, historyResponse{Messages: messagesToResponse(msgs)}, http.StatusOK)
}

// handleAddChannelMember handles POST /api/v1/channels/{id}/members.
func (s *Server) handleAddChannelMember(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	id := r.PathValue("id")
	var req addMemberRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.ID == "" {
		writeError(w, "BAD_REQUEST", "id is required", http.StatusBadRequest)
		return
	}
	policy := channels.RespondWhenMentioned
	if req.Respond != "" {
		policy = channels.RespondPolicy(req.Respond)
	}
	if err := s.channelStore.AddMember(r.Context(), id, req.ID, policy); err != nil {
		s.writeChannelError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// channelToResponse converts a [channels.Channel] (and an optional
// member slice) to the wire shape.
func channelToResponse(ch channels.Channel, members []channels.Member) channelResponse {
	out := channelResponse{
		ID:          ch.ID,
		Name:        ch.Name,
		Type:        string(ch.Type),
		Description: ch.Description,
		CreatedAt:   ch.CreatedAt,
	}
	if members != nil {
		out.Members = make([]memberResponse, 0, len(members))
		for _, m := range members {
			out.Members = append(out.Members, memberResponse{
				ID:            m.ParticipantID,
				RespondPolicy: string(m.RespondPolicy),
				JoinedAt:      m.JoinedAt,
			})
		}
	}
	return out
}

func messageToResponse(m channels.ChannelMessage) channelMessageResponse {
	out := channelMessageResponse{
		ID:        m.ID,
		ChannelID: m.ChannelID,
		SenderID:  m.SenderID,
		Content:   m.Content,
		Timestamp: m.Timestamp,
		ThreadID:  m.ThreadID,
		Mentions:  m.Mentions,
		Metadata:  m.Metadata,
	}
	if out.Mentions == nil {
		out.Mentions = []string{}
	}
	return out
}

func messagesToResponse(in []channels.ChannelMessage) []channelMessageResponse {
	out := make([]channelMessageResponse, 0, len(in))
	for _, m := range in {
		out = append(out, messageToResponse(m))
	}
	return out
}

// writeChannelError maps a channels package error to the standard JSON
// envelope and HTTP status. Centralised so every handler reports the
// same code/message for the same store sentinel.
func (s *Server) writeChannelError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, channels.ErrChannelNotFound):
		writeError(w, "NOT_FOUND", "channel not found", http.StatusNotFound)
	case errors.Is(err, channels.ErrMessageNotFound):
		writeError(w, "NOT_FOUND", "message not found", http.StatusNotFound)
	case errors.Is(err, channels.ErrChannelExists):
		writeError(w, "CONFLICT", "channel already exists", http.StatusConflict)
	case errors.Is(err, channels.ErrChannelCapExceeded):
		writeError(w, "CONFLICT", "max_channels cap exceeded", http.StatusConflict)
	case errors.Is(err, channels.ErrNotMember):
		writeError(w, "FORBIDDEN", "sender is not a member of the channel", http.StatusForbidden)
	case errors.Is(err, channels.ErrInvalidChannelType),
		errors.Is(err, channels.ErrInvalidParticipantID),
		errors.Is(err, channels.ErrInvalidRespondPolicy):
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
	default:
		s.logger.Error("channels: unexpected error", zap.Error(err))
		writeError(w, "INTERNAL", "channel store error", http.StatusInternalServerError)
	}
}

// parseLimit parses the optional `?limit=` query parameter, returning
// `fallback` when absent. PR #245 review (Low): a non-empty malformed
// value (`abc`, `-5`, `0`) used to be silently coerced to the fallback.
// That hides client bugs and conflicts with the parseBefore convention
// just below (which errors loudly on a malformed `?before=`). We now
// return an error so the caller can surface 400 BAD_REQUEST. Values
// above [channelMaxLimit] are still capped silently — that is the
// documented contract (the cap exists to bound allocation, not to
// signal a client bug).
func parseLimit(r *http.Request, fallback int) (int, error) {
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return fallback, nil
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("limit must be a positive integer: %s", raw)
	}
	if v <= 0 {
		return 0, fmt.Errorf("limit must be a positive integer: %d", v)
	}
	if v > channelMaxLimit {
		return channelMaxLimit, nil
	}
	return v, nil
}

// parseBefore parses the optional `before` cursor as RFC 3339. Returns
// the zero value (sentinel for "now") when the parameter is absent.
// Errors out on a malformed value rather than silently treating it as
// "now" — drift between the cursor format and the response timestamp
// format would be hard to debug.
func parseBefore(r *http.Request) (time.Time, error) {
	raw := r.URL.Query().Get("before")
	if raw == "" {
		return time.Time{}, nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, fmt.Errorf("before must be RFC 3339: %w", err)
	}
	return t.UTC(), nil
}
