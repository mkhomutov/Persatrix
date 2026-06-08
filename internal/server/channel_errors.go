package server

import (
	"errors"
	"net/http"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

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
	case errors.Is(err, channels.ErrMessageContentTooLarge): // ISSUE-0050
		writeError(w, "PAYLOAD_TOO_LARGE", err.Error(), http.StatusRequestEntityTooLarge)
	case errors.Is(err, channels.ErrParticipantBudgetExhausted): // RFC 0030 Layer 2
		writeError(w, "TOO_MANY_REQUESTS", err.Error(), http.StatusTooManyRequests)
	default:
		s.logger.Error("channels: unexpected error", zap.Error(err))
		writeError(w, "INTERNAL", "channel store error", http.StatusInternalServerError)
	}
}
