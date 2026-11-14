package server

import (
	"fmt"
	"net/http"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// handleGetThread handles GET /api/v1/channels/{id}/messages/{msg_id}/thread.
//
// The message must be in the channel the path names (ISSUE-0203). The
// response is labelled with that channel's classification, so a message of
// another channel would be shown under a level that is not its own. Such a
// message answers 404 with the same body as an id that does not exist, so
// the route does not say which ids exist in other channels.
//
// Moved here from channel_handlers.go, which was at the 500-line size
// warning when the channel check was added.
func (s *Server) handleGetThread(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	chID := r.PathValue("id")
	msgID := r.PathValue("msg_id")
	ch, err := s.channelStore.GetChannel(r.Context(), chID)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	parent, err := s.channelStore.GetMessage(r.Context(), msgID)
	if err == nil && parent.ChannelID != ch.ID {
		err = fmt.Errorf("%w: %s is not in %s", channels.ErrMessageNotFound, msgID, ch.ID)
	}
	if err != nil {
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
	// RFC 0037 §B/§H: a thread is never more or less confidential than the
	// channel it forks from — the parent channel's level, like history.
	writeJSON(w, historyResponse{
		Messages:       messagesToResponse(msgs),
		Classification: string(ch.Classification),
	}, http.StatusOK)
}
